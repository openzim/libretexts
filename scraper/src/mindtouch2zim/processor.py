import argparse
import datetime
import json
import re
from io import BytesIO
from pathlib import Path

from pydantic import BaseModel
from requests.exceptions import HTTPError
from schedule import every, run_pending
from zimscraperlib.download import (
    stream_file,  # pyright: ignore[reportUnknownVariableType]
)
from zimscraperlib.image import convert_image, resize_image
from zimscraperlib.image.conversion import convert_svg2png
from zimscraperlib.image.probing import format_for
from zimscraperlib.rewriting.css import CssRewriter
from zimscraperlib.rewriting.html import HtmlRewriter
from zimscraperlib.rewriting.html import rules as html_rules
from zimscraperlib.rewriting.url_rewriting import (
    ArticleUrlRewriter,
    HttpUrl,
    RewriteResult,
    ZimPath,
)
from zimscraperlib.zim import Creator
from zimscraperlib.zim.filesystem import validate_zimfile_creatable
from zimscraperlib.zim.indexing import IndexData

from mindtouch2zim.client import (
    LibraryPage,
    LibraryPageId,
    LibraryTree,
    MindtouchClient,
    MindtouchHome,
)
from mindtouch2zim.constants import LANGUAGE_ISO_639_3, NAME, VERSION, logger
from mindtouch2zim.ui import (
    ConfigModel,
    PageContentModel,
    PageModel,
    SharedModel,
)
from mindtouch2zim.zimconfig import ZimConfig


class InvalidFormatError(Exception):
    """Raised when a user supplied template has an invalid parameter."""

    pass


class MissingDocumentError(Exception):
    """Raised when the user specified a slug that doesn't exist."""

    pass


class UnsupportedTagError(Exception):
    """An exception raised when an HTML tag is not expected to be encountered"""

    pass


class NoIllustrationFoundError(Exception):
    """An exception raised when no suitable illustration has been found"""

    pass


class ContentFilter(BaseModel):
    """Supports filtering documents by user provided attributes."""

    # If specified, only pages with title matching the regex are included.
    page_title_include: str | None
    # If specified, only page with matching ids are included.
    page_id_include: str | None
    # If specified, page with title matching the regex are excluded.
    page_title_exclude: str | None
    # If specified, only this page and its subpages will be included.
    root_page_id: str | None

    @staticmethod
    def of(namespace: argparse.Namespace) -> "ContentFilter":
        """Parses a namespace to create a new DocFilter."""
        return ContentFilter.model_validate(namespace, from_attributes=True)

    def filter(self, page_tree: LibraryTree) -> list[LibraryPage]:
        """Filters pages based on the user's choices."""

        if self.root_page_id:
            page_tree = page_tree.sub_tree(self.root_page_id)

        title_include_re = (
            re.compile(self.page_title_include, re.IGNORECASE)
            if self.page_title_include
            else None
        )
        title_exclude_re = (
            re.compile(self.page_title_exclude, re.IGNORECASE)
            if self.page_title_exclude
            else None
        )
        id_include = (
            [page_id.strip() for page_id in self.page_id_include.split(",")]
            if self.page_id_include
            else None
        )

        def is_selected(
            title_include_re: re.Pattern[str] | None,
            title_exclude_re: re.Pattern[str] | None,
            id_include: list[LibraryPageId] | None,
            page: LibraryPage,
        ) -> bool:
            return (
                (
                    not title_include_re
                    or title_include_re.search(page.title) is not None
                )
                and (not id_include or page.id in id_include)
                and (
                    not title_exclude_re or title_exclude_re.search(page.title) is None
                )
            )

        # Find selected pages and their parent, and create a set of unique ids
        selected_ids = {
            selected_page.id
            for page in page_tree.pages.values()
            for selected_page in page.self_and_parents
            if is_selected(title_include_re, title_exclude_re, id_include, page)
        }

        # Then transform set of ids into list of pages
        return [page for page in page_tree.pages.values() if page.id in selected_ids]


def add_item_for(
    creator: Creator,
    path: str,
    title: str | None = None,
    *,
    fpath: Path | None = None,
    content: bytes | str | None = None,
    mimetype: str | None = None,
    is_front: bool | None = None,
    should_compress: bool | None = None,
    delete_fpath: bool | None = False,
    duplicate_ok: bool | None = None,
    index_data: IndexData | None = None,
    auto_index: bool = True,
):
    """
    Boilerplate to avoid repeating pyright ignore

    To be removed, once upstream issue is solved, see
    https://github.com/openzim/libretexts/issues/26
    """
    creator.add_item_for(  # pyright: ignore[reportUnknownMemberType]
        path=path,
        title=title,
        fpath=fpath,
        content=content,
        mimetype=mimetype,
        is_front=is_front,
        should_compress=should_compress,
        delete_fpath=delete_fpath,
        duplicate_ok=duplicate_ok,
        index_data=index_data,
        auto_index=auto_index,
    )


class Processor:
    """Generates ZIMs based on the user's configuration."""

    def __init__(
        self,
        mindtouch_client: MindtouchClient,
        zim_config: ZimConfig,
        content_filter: ContentFilter,
        output_folder: Path,
        zimui_dist: Path,
        stats_file: Path | None,
        illustration_url: str | None,
        *,
        overwrite_existing_zim: bool,
    ) -> None:
        """Initializes Processor.

        Parameters:
            libretexts_client: Client that connects with Libretexts website
            zim_config: Configuration for ZIM metadata.
            content_filter: User supplied filter selecting with content to convert.
            output_folder: Directory to write ZIMs into.
            zimui_dist: Build directory where Vite placed compiled Vue.JS frontend.
            stats_file: Path where JSON task progress while be saved.
            overwrite_existing_zim: Do not fail if ZIM already exists, overwrite it.
        """
        self.mindtouch_client = mindtouch_client
        self.zim_config = zim_config
        self.content_filter = content_filter
        self.output_folder = output_folder
        self.zimui_dist = zimui_dist
        self.stats_file = stats_file
        self.overwrite_existing_zim = overwrite_existing_zim
        self.illustration_url = illustration_url

        self.stats_items_done = 0
        # we add 1 more items to process so that progress is not 100% at the beginning
        # when we do not yet know how many items we have to process and so that we can
        # increase counter at the beginning of every for loop, not minding about what
        # could happen in the loop in terms of exit conditions
        self.stats_items_total = 1

    def run(self) -> Path:
        """Generates a zim for a single document.

        Returns the path to the gernated ZIM.
        """
        logger.info("Generating ZIM")

        # create first progress report and and a timer to update every 10 seconds
        self._report_progress()
        every(10).seconds.do(  # pyright: ignore[reportUnknownMemberType]
            self._report_progress
        )

        formatted_config = self.zim_config.format(
            {
                "name": self.zim_config.name,
                "period": datetime.date.today().strftime("%Y-%m"),
            }
        )
        zim_file_name = f"{formatted_config.file_name}.zim"
        zim_path = self.output_folder / zim_file_name

        if zim_path.exists():
            if self.overwrite_existing_zim:
                zim_path.unlink()
            else:
                logger.error(f"  {zim_path} already exists, aborting.")
                raise SystemExit(2)

        validate_zimfile_creatable(self.output_folder, zim_file_name)

        logger.info(f"  Writing to: {zim_path}")

        creator = Creator(zim_path, "index.html")

        logger.info("  Fetching and storing home page...")
        home = self.mindtouch_client.get_home()

        logger.info("  Fetching ZIM illustration...")
        zim_illustration = self._fetch_zim_illustration(home)

        logger.debug("Configuring metadata")
        creator.config_metadata(
            Name=formatted_config.name,
            Title=formatted_config.title,
            Publisher=formatted_config.publisher,
            Date=datetime.datetime.now(tz=datetime.UTC).date(),
            Creator=formatted_config.creator,
            Description=formatted_config.description,
            LongDescription=formatted_config.long_description,
            # As of 2024-09-4 all documentation is in English.
            Language=LANGUAGE_ISO_639_3,
            Tags=formatted_config.tags,
            Scraper=f"{NAME} v{VERSION}",
            Illustration_48x48_at_1=zim_illustration.getvalue(),
        )

        # Start creator early to detect problems early.
        with creator as creator:

            add_item_for(
                creator,
                "favicon.ico",
                content=self._fetch_favicon_from_illustration(
                    zim_illustration
                ).getvalue(),
            )
            del zim_illustration

            logger.info("  Storing configuration...")
            add_item_for(
                creator,
                "content/config.json",
                content=ConfigModel(
                    secondary_color=self.zim_config.secondary_color
                ).model_dump_json(by_alias=True),
            )

            count_zimui_files = len(list(self.zimui_dist.rglob("*")))
            logger.info(
                f"Adding {count_zimui_files} Vue.JS UI files in {self.zimui_dist}"
            )
            self.stats_items_total += count_zimui_files
            for file in self.zimui_dist.rglob("*"):
                self.stats_items_done += 1
                run_pending()
                if file.is_dir():
                    continue
                path = str(Path(file).relative_to(self.zimui_dist))
                logger.debug(f"Adding {path} to ZIM")
                if path == "index.html":  # Change index.html title and add to ZIM
                    index_html_path = self.zimui_dist / path
                    add_item_for(
                        creator=creator,
                        path=path,
                        content=index_html_path.read_text(encoding="utf-8").replace(
                            "<title>Vite App</title>",
                            f"<title>{formatted_config.title}</title>",
                        ),
                        mimetype="text/html",
                        is_front=True,
                    )
                else:
                    add_item_for(
                        creator=creator,
                        path=path,
                        fpath=file,
                        is_front=False,
                    )

            mathjax = (Path(__file__) / "../mathjax").resolve()
            count_mathjax_files = len(list(mathjax.rglob("*")))
            self.stats_items_total += count_mathjax_files
            logger.info(f"Adding {count_mathjax_files} MathJax files in {mathjax}")
            for file in mathjax.rglob("*"):
                self.stats_items_done += 1
                run_pending()
                if not file.is_file():
                    continue
                path = str(Path(file).relative_to(mathjax.parent))
                logger.debug(f"Adding {path} to ZIM")
                add_item_for(
                    creator=creator,
                    path=path,
                    fpath=file,
                    is_front=False,
                )

            welcome_image = BytesIO()
            stream_file(home.welcome_image_url, byte_stream=welcome_image)
            add_item_for(creator, "content/logo.png", content=welcome_image.getvalue())
            del welcome_image

            self.items_to_download: dict[ZimPath, set[HttpUrl]] = {}
            self._process_css(
                css_location=home.screen_css_url,
                target_filename="screen.css",
                creator=creator,
            )
            self._process_css(
                css_location=home.print_css_url,
                target_filename="print.css",
                creator=creator,
            )
            self._process_css(
                css_location=home.home_url,
                css_content="\n".join(home.inline_css),
                target_filename="inline.css",
                creator=creator,
            )

            logger.info("Fetching pages tree")
            pages_tree = self.mindtouch_client.get_page_tree()
            selected_pages = self.content_filter.filter(pages_tree)
            logger.info(
                f"{len(selected_pages)} pages (out of {len(pages_tree.pages)}) will be "
                "fetched and pushed to the ZIM"
            )
            add_item_for(
                creator,
                "content/shared.json",
                content=SharedModel(
                    logo_path="content/logo.png",
                    root_page_path=selected_pages[0].path,  # root is always first
                    pages=[
                        PageModel(id=page.id, title=page.title, path=page.path)
                        for page in selected_pages
                    ],
                ).model_dump_json(by_alias=True),
            )

            logger.info("Fetching pages content")
            # compute the list of existing pages to properly rewrite links leading
            # in-ZIM / out-of-ZIM
            self.stats_items_total += len(selected_pages)
            existing_html_pages = {
                ArticleUrlRewriter.normalize(
                    HttpUrl(f"{self.mindtouch_client.library_url}/{page.path}")
                )
                for page in selected_pages
            }
            for page in selected_pages:
                self.stats_items_done += 1
                run_pending()
                self._process_page(
                    creator=creator, page=page, existing_zim_paths=existing_html_pages
                )

            logger.info(f"  Retrieving {len(self.items_to_download)} assets...")
            self.stats_items_total += len(self.items_to_download)
            for asset_path, asset_urls in self.items_to_download.items():
                self.stats_items_done += 1
                run_pending()
                for asset_url in asset_urls:
                    try:
                        asset_content = BytesIO()
                        stream_file(asset_url.value, byte_stream=asset_content)
                        logger.debug(
                            f"Adding {asset_url.value} to {asset_path.value} in the ZIM"
                        )
                        add_item_for(
                            creator,
                            "content/" + asset_path.value,
                            content=asset_content.getvalue(),
                        )
                        break  # file found and added
                    except HTTPError as exc:
                        # would make more sense to be a warning, but this is just too
                        # verbose, at least on geo.libretexts.org many assets are just
                        # missing
                        logger.debug(f"Ignoring {asset_path.value} due to {exc}")

        # same reason than self.stats_items_done = 1 at the beginning, we need to add
        # a final item to complete the progress
        self.stats_items_done += 1
        self._report_progress()

        return zim_path

    def _process_css(
        self,
        creator: Creator,
        target_filename: str,
        css_location: str,
        css_content: str | bytes | None = None,
    ):
        """Process a given CSS stylesheet
        Download content if necessary, rewrite CSS and add CSS to ZIM
        """
        if not css_location:
            raise ValueError(f"Cannot process empty css_location for {target_filename}")
        if not css_content:
            css_buffer = BytesIO()
            stream_file(css_location, byte_stream=css_buffer)
            css_content = css_buffer.getvalue()
        url_rewriter = CssUrlsRewriter(
            article_url=HttpUrl(css_location),
            article_path=ZimPath(target_filename),
        )
        css_rewriter = CssRewriter(
            url_rewriter=url_rewriter, base_href=None, remove_errors=True
        )
        result = css_rewriter.rewrite(content=css_content)
        # Rebuild the dict since we might have "conflict" of ZimPath (two urls leading
        # to the same ZimPath) and we prefer to use the first URL encountered, where
        # using self.items_to_download.update while override the key value, prefering
        # to use last URL encountered.
        for path, urls in url_rewriter.items_to_download.items():
            if path in self.items_to_download:
                self.items_to_download[path].update(urls)
            else:
                self.items_to_download[path] = urls
        add_item_for(creator, f"content/{target_filename}", content=result)

    def _process_page(
        self, creator: Creator, page: LibraryPage, existing_zim_paths: set[ZimPath]
    ):
        """Process a given library page
        Download content, rewrite HTML and add JSON to ZIM
        """
        logger.debug(f"  Fetching {page.id}")
        page_content = self.mindtouch_client.get_page_content(page)
        url_rewriter = HtmlUrlsRewriter(
            self.mindtouch_client.library_url,
            page,
            existing_zim_paths=existing_zim_paths,
        )
        rewriter = HtmlRewriter(
            url_rewriter=url_rewriter,
            pre_head_insert=None,
            post_head_insert=None,
            notify_js_module=None,
        )
        rewriten = rewriter.rewrite(page_content.html_body)
        for path, urls in url_rewriter.items_to_download.items():
            if path in self.items_to_download:
                self.items_to_download[path].update(urls)
            else:
                self.items_to_download[path] = urls
        add_item_for(
            creator,
            f"content/page_content_{page.id}.json",
            content=PageContentModel(html_body=rewriten.content).model_dump_json(
                by_alias=True
            ),
        )

    def _report_progress(self):
        """report progress to stats file"""

        logger.info(f"  Progress {self.stats_items_done} / {self.stats_items_total}")
        if not self.stats_file:
            return
        progress = {
            "done": self.stats_items_done,
            "total": self.stats_items_total,
        }
        self.stats_file.write_text(json.dumps(progress, indent=2))

    def _fetch_zim_illustration(self, home: MindtouchHome) -> BytesIO:
        """Fetch ZIM illustration, convert/resize and return it"""
        for icon_url in (
            [self.illustration_url] if self.illustration_url else home.icons_urls
        ):
            try:
                logger.debug(f"Downloading {icon_url} illustration")
                illustration_content = BytesIO()
                stream_file(icon_url, byte_stream=illustration_content)
                illustration_format = format_for(
                    illustration_content, from_suffix=False
                )
                png_illustration = BytesIO()
                if illustration_format == "SVG":
                    logger.debug("Converting SVG illustration to PNG")
                    convert_svg2png(illustration_content, png_illustration, 48, 48)
                elif illustration_format == "PNG":
                    png_illustration = illustration_content
                else:
                    logger.debug(
                        f"Converting {illustration_format} illustration to PNG"
                    )
                    convert_image(illustration_content, png_illustration, fmt="PNG")
                logger.debug("Resizing ZIM illustration")
                resize_image(
                    src=png_illustration,
                    width=48,
                    height=48,
                    method="cover",
                )
                return png_illustration
            except Exception as exc:
                logger.warning(
                    f"Failed to retrieve illustration at {icon_url}", exc_info=exc
                )
        raise NoIllustrationFoundError("Failed to find a suitable illustration")

    def _fetch_favicon_from_illustration(self, illustration: BytesIO) -> BytesIO:
        """Return a converted version of the illustration into favicon"""
        favicon = BytesIO()
        convert_image(illustration, favicon, fmt="ICO")
        logger.debug("Resizing ZIM illustration")
        resize_image(
            src=favicon,
            width=32,
            height=32,
            method="cover",
        )
        return favicon


# remove all standard rules, they are not adapted to Vue.JS UI
html_rules.rewrite_attribute_rules.clear()
html_rules.rewrite_data_rules.clear()
html_rules.rewrite_tag_rules.clear()


@html_rules.rewrite_attribute()
def rewrite_href_src_attributes(
    tag: str,
    attr_name: str,
    attr_value: str | None,
    url_rewriter: ArticleUrlRewriter,
    base_href: str | None,
):
    """Rewrite href and src attributes"""
    if attr_name not in ("href", "src") or not attr_value:
        return
    if not isinstance(url_rewriter, HtmlUrlsRewriter):
        raise Exception("Expecting HtmlUrlsRewriter")
    new_attr_value = None
    if tag == "a":
        rewrite_result = url_rewriter(
            attr_value, base_href=base_href, rewrite_all_url=False
        )
        # rewrite links for proper navigation inside ZIM Vue.JS UI (if inside ZIM) or
        # full link (if outside the current library)
        new_attr_value = (
            f"#/{rewrite_result.rewriten_url[len(url_rewriter.library_path.value) :]}"
            if rewrite_result.rewriten_url.startswith(url_rewriter.library_path.value)
            else rewrite_result.rewriten_url
        )
    if tag == "img":
        rewrite_result = url_rewriter(
            attr_value, base_href=base_href, rewrite_all_url=True
        )
        # add 'content/' to the URL since all assets will be stored in the sub.-path
        new_attr_value = f"content/{rewrite_result.rewriten_url}"
        url_rewriter.add_item_to_download(rewrite_result)
    if not new_attr_value:
        # we do not (yet) support other tags / attributes so we fail the scraper
        raise ValueError(
            f"Empty new value when rewriting {attr_value} from {attr_name} in {tag} tag"
        )
    return (attr_name, new_attr_value)


@html_rules.drop_attribute()
def drop_sizes_and_srcset_attribute(tag: str, attr_name: str):
    """Drop srcset and sizes attributes in <img> tags"""
    return tag == "img" and attr_name in ("srcset", "sizes")


@html_rules.rewrite_tag()
def refuse_unsupported_tags(tag: str):
    """Stop scraper if unsupported tag is encountered"""
    if tag not in ["picture"]:
        return
    raise UnsupportedTagError(f"Tag {tag} is not yet supported in this scraper")


class HtmlUrlsRewriter(ArticleUrlRewriter):
    """A rewriter for HTML processing

    This rewriter does not store items to download on-the-fly but has containers and
    metadata so that HTML rewriting rules can decide what needs to be downloaded
    """

    def __init__(
        self, library_url: str, page: LibraryPage, existing_zim_paths: set[ZimPath]
    ):
        super().__init__(
            article_url=HttpUrl(f"{library_url}/{page.path}"),
            article_path=ZimPath("index.html"),
            existing_zim_paths=existing_zim_paths,
        )
        self.library_url = library_url
        self.library_path = ArticleUrlRewriter.normalize(HttpUrl(f"{library_url}/"))
        self.items_to_download: dict[ZimPath, set[HttpUrl]] = {}

    def __call__(
        self, item_url: str, base_href: str | None, *, rewrite_all_url: bool = True
    ) -> RewriteResult:
        result = super().__call__(item_url, base_href, rewrite_all_url=rewrite_all_url)
        return result

    def add_item_to_download(self, rewrite_result: RewriteResult):
        """Add item to download based on rewrite result"""
        if rewrite_result.zim_path is not None:
            # if item is expected to be inside the ZIM, store asset information so that
            # we can download it afterwards
            if rewrite_result.zim_path in self.items_to_download:
                self.items_to_download[rewrite_result.zim_path].add(
                    HttpUrl(rewrite_result.absolute_url)
                )
            else:
                self.items_to_download[rewrite_result.zim_path] = {
                    HttpUrl(rewrite_result.absolute_url)
                }


class CssUrlsRewriter(ArticleUrlRewriter):
    """A rewriter for CSS processing, storing items to download as URL as processed"""

    def __init__(
        self,
        *,
        article_url: HttpUrl,
        article_path: ZimPath,
    ):
        super().__init__(
            article_url=article_url,
            article_path=article_path,
        )
        self.items_to_download: dict[ZimPath, set[HttpUrl]] = {}

    def __call__(
        self,
        item_url: str,
        base_href: str | None,
        *,
        rewrite_all_url: bool = True,  # noqa: ARG002
    ) -> RewriteResult:
        result = super().__call__(item_url, base_href, rewrite_all_url=True)
        if result.zim_path is None:
            return result
        if result.zim_path in self.items_to_download:
            self.items_to_download[result.zim_path].add(HttpUrl(result.absolute_url))
        else:
            self.items_to_download[result.zim_path] = {HttpUrl(result.absolute_url)}
        return result
