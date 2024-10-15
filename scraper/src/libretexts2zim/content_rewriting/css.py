import re
from collections.abc import Iterable
from functools import partial
from typing import Any

from tinycss2 import (
    ast,
    parse_declaration_list,  # pyright: ignore[reportUnknownVariableType]
    parse_stylesheet,  # pyright: ignore[reportUnknownVariableType]
    parse_stylesheet_bytes,  # pyright: ignore[reportUnknownVariableType]
    serialize,  # pyright: ignore[reportUnknownVariableType]
)
from tinycss2.serializer import (
    serialize_url,  # pyright: ignore[reportUnknownVariableType]
)

from libretexts2zim.constants import logger
from libretexts2zim.content_rewriting.rx_replacer import RxRewriter, TransformationRule
from libretexts2zim.content_rewriting.url_rewriting import ArticleUrlRewriter


class FallbackRegexCssRewriter(RxRewriter):

    def simple_transform(
        self,
        url_rewriter: ArticleUrlRewriter,
        base_href: str | None,
        m_object: re.Match[str],
        _opts: dict[str, Any] | None,
    ) -> str:
        return "".join(
            [
                "url(",
                m_object["quote"],
                url_rewriter(m_object["url"], base_href),
                m_object["quote"],
                ")",
            ]
        )

    def __init__(self, url_rewriter: ArticleUrlRewriter, base_href: str | None):
        rules = [
            TransformationRule(
                [
                    re.compile(""),
                    partial(
                        self.simple_transform,
                        url_rewriter=url_rewriter,
                        base_href=base_href,
                    ),
                ]
            )
        ]
        super().__init__(rules)


class CssRewriter:
    def __init__(self, url_rewriter: ArticleUrlRewriter, base_href: str | None):
        self.url_rewriter = url_rewriter
        self.base_href = base_href
        self.fallback_rewriter = FallbackRegexCssRewriter(url_rewriter, base_href)

    def _serialize_rules(self, rules: list[ast.Node]) -> str:
        return serialize(
            [rule for rule in rules if not isinstance(rule, ast.ParseError)]
        )

    def rewrite(self, content: str | bytes) -> str:
        try:
            if isinstance(content, bytes):
                rules, _ = (  # pyright: ignore[reportUnknownVariableType]
                    parse_stylesheet_bytes(content)
                )

            else:
                rules = parse_stylesheet(  # pyright: ignore[reportUnknownVariableType]
                    content
                )
            self._process_list(rules)  # pyright: ignore[reportUnknownArgumentType]

            return self._serialize_rules(
                rules  # pyright: ignore[reportUnknownArgumentType]
            )
        except Exception:
            # If tinycss fail to parse css, it will generate a "Error" token.
            # Exception is raised at serialization time.
            # We try/catch the whole process to be sure anyway.
            logger.warning(
                (
                    "Css transformation fails. Fallback to regex rewriter.\n"
                    "Article path is %s"
                ),
                self.url_rewriter.article_url,
            )
            return self.fallback_rewriter.rewrite(content, {})

    def rewrite_inline(self, content: str) -> str:
        try:
            rules = (  # pyright: ignore[reportUnknownVariableType]
                parse_declaration_list(content)
            )
            self._process_list(rules)  # pyright: ignore[reportUnknownArgumentType]
            return self._serialize_rules(
                rules  # pyright: ignore[reportUnknownArgumentType]
            )
        except Exception:
            # If tinycss fail to parse css, it will generate a "Error" token.
            # Exception is raised at serialization time.
            # We try/catch the whole process to be sure anyway.
            logger.warning(
                (
                    "Css transformation fails. Fallback to regex rewriter.\n"
                    "Content is `%s`"
                ),
                content,
            )
            return self.fallback_rewriter.rewrite(content, {})

    def _process_list(self, nodes: Iterable[ast.Node] | None):
        """Process a list of CSS nodes"""
        if not nodes:
            return
        for node in nodes:
            self._process_node(node)

    def _process_node(self, node: ast.Node):
        """Process one single CSS node"""
        if isinstance(
            node,
            ast.QualifiedRule
            | ast.SquareBracketsBlock
            | ast.ParenthesesBlock
            | ast.CurlyBracketsBlock,
        ):
            self._process_list(
                node.content,  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            )
        elif isinstance(node, ast.FunctionBlock):
            if node.lower_name == "url":  # pyright: ignore[reportUnknownMemberType]
                url_node: ast.Node = node.arguments[0]  # pyright: ignore
                relative_css_path = self.url_rewriter(
                    url_node.value,  # pyright: ignore
                    self.base_href,
                )
                if not relative_css_path:
                    return
                url_node.value = str(relative_css_path)  # pyright: ignore
                url_node.representation = (  # pyright: ignore
                    f'"{serialize_url(str(relative_css_path))}"'
                )

            else:
                self._process_list(
                    node.arguments,  # pyright: ignore
                )
        elif isinstance(node, ast.AtRule):
            self._process_list(node.prelude)  # pyright: ignore
            self._process_list(node.content)  # pyright: ignore
        elif isinstance(node, ast.Declaration):
            self._process_list(node.value)  # pyright: ignore
        elif isinstance(node, ast.URLToken):
            new_url = self.url_rewriter(node.value, self.base_href)  # pyright: ignore
            node.value = new_url
            node.representation = f"url({serialize_url(new_url)})"
