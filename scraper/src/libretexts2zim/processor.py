import argparse
import datetime
import re
from io import BytesIO
from pathlib import Path

from pydantic import BaseModel
from requests.exceptions import HTTPError
from zimscraperlib.download import (
    stream_file,  # pyright: ignore[reportUnknownVariableType]
)
from zimscraperlib.image import resize_image
from zimscraperlib.zim import Creator
from zimscraperlib.zim.filesystem import validate_zimfile_creatable
from zimscraperlib.zim.indexing import IndexData

from libretexts2zim.client import (
    LibraryPage,
    LibraryPageId,
    LibraryTree,
    LibreTextsClient,
    LibreTextsMetadata,
)
from libretexts2zim.constants import LANGUAGE_ISO_639_3, NAME, ROOT_DIR, VERSION, logger
from libretexts2zim.css import CssProcessor
from libretexts2zim.ui import (
    ConfigModel,
    PageContentModel,
    PageModel,
    SharedModel,
)
from libretexts2zim.zimconfig import ZimConfig


class InvalidFormatError(Exception):
    """Raised when a user supplied template has an invalid parameter."""

    pass


class MissingDocumentError(Exception):
    """Raised when the user specified a slug that doesn't exist."""

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
        libretexts_client: LibreTextsClient,
        zim_config: ZimConfig,
        content_filter: ContentFilter,
        output_folder: Path,
        zimui_dist: Path,
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
            overwrite_existing_zim: Do not fail if ZIM already exists, overwrite it.
        """
        self.libretexts_client = libretexts_client
        self.zim_config = zim_config
        self.content_filter = content_filter
        self.output_folder = output_folder
        self.zimui_dist = zimui_dist
        self.overwrite_existing_zim = overwrite_existing_zim

        self.zim_illustration_path = self.libretexts_newsite_path(
            "header_logo_mini.png"
        )

    @staticmethod
    def libretexts_newsite_path(name: str) -> Path:
        """Returns the path to name in the third_party/libretexts_newsite folder.

        Raises ValueError if the resource doesn't exist.
        """
        path = ROOT_DIR.joinpath("third_party", "libretexts_newsite", name)
        if not path.exists():
            raise ValueError(f"File not found at {path}")
        return path

    def run(self) -> Path:
        """Generates a zim for a single document.

        Returns the path to the gernated ZIM.
        """
        logger.info("Generating ZIM")

        metadata = LibreTextsMetadata(
            name=self.zim_config.library_name, slug=self.libretexts_client.library_slug
        )
        formatted_config = self.zim_config.format(metadata.placeholders())
        zim_file_name = f"{formatted_config.file_name_format}.zim"
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

        logger.debug("Resizing ZIM illustration")
        zim_illustration = BytesIO()
        resize_image(
            src=self.zim_illustration_path,
            dst=zim_illustration,
            width=48,
            height=48,
            method="cover",
        )

        logger.debug("Configuring metadata")
        creator.config_metadata(
            Name=formatted_config.name_format,
            Title=formatted_config.title_format,
            Publisher=formatted_config.publisher,
            Date=datetime.datetime.now(tz=datetime.UTC).date(),
            Creator=formatted_config.creator,
            Description=formatted_config.description_format,
            LongDescription=formatted_config.long_description_format,
            # As of 2024-09-4 all documentation is in English.
            Language=LANGUAGE_ISO_639_3,
            Tags=formatted_config.tags,
            Scraper=f"{NAME} v{VERSION}",
            Illustration_48x48_at_1=zim_illustration.getvalue(),
        )
        del zim_illustration

        # Start creator early to detect problems early.
        with creator as creator:

            logger.info("  Storing configuration...")
            add_item_for(
                creator,
                "content/config.json",
                content=ConfigModel(
                    secondary_color=self.zim_config.secondary_color
                ).model_dump_json(by_alias=True),
            )

            logger.info(f"Adding Vue.JS UI files in {self.zimui_dist}")
            for file in self.zimui_dist.rglob("*"):
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
                            f"<title>{formatted_config.title_format}</title>",
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
            logger.info(f"Adding MathJax files in {mathjax}")
            for file in mathjax.rglob("*"):
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

            logger.info("  Fetching and storing home page...")
            home = self.libretexts_client.get_home()

            welcome_image = BytesIO()
            stream_file(home.welcome_image_url, byte_stream=welcome_image)
            add_item_for(creator, "content/logo.png", content=welcome_image.getvalue())
            del welcome_image

            css_processor = CssProcessor()
            screen_css = BytesIO()
            stream_file(home.screen_css_url, byte_stream=screen_css)
            result = css_processor.process(
                css_original_url=home.screen_css_url, css_content=screen_css.getvalue()
            )
            add_item_for(creator, "content/screen.css", content=result)
            del screen_css

            print_css = BytesIO()
            stream_file(home.print_css_url, byte_stream=print_css)
            result = css_processor.process(
                css_original_url=home.print_css_url, css_content=print_css.getvalue()
            )
            add_item_for(creator, "content/print.css", content=result)
            del print_css

            result = css_processor.process(
                css_original_url=home.home_url,
                css_content=("\n".join(home.inline_css)).encode(),
            )
            add_item_for(creator, "content/inline.css", content=result)

            logger.info(f"  Retrieving {len(css_processor.css_assets)} CSS assets...")
            for asset_url, asset_path in css_processor.css_assets.items():
                try:
                    css_asset = BytesIO()
                    stream_file(asset_url, byte_stream=css_asset)
                    add_item_for(
                        creator, str(asset_path)[1:], content=css_asset.getvalue()
                    )
                    logger.debug(f"Adding {asset_url} to {asset_path} in the ZIM")
                    del css_asset
                except HTTPError as exc:
                    # would make more sense to be a warning, but this is just too
                    # verbose, at least on geo.libretexts.org many assets are just
                    # missing
                    logger.debug(f"Ignoring {asset_path} due to {exc}")

            logger.info("Fetching pages tree")
            pages_tree = self.libretexts_client.get_page_tree()
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
            for page in selected_pages:
                logger.debug(f"  Fetching {page.id}")
                page_content = self.libretexts_client.get_page_content(page)
                add_item_for(
                    creator,
                    f"content/page_content_{page.id}.json",
                    content=PageContentModel(
                        html_body=page_content.html_body
                    ).model_dump_json(by_alias=True),
                )

        return zim_path
