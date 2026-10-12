"""Dictionary-attached derived senses / 词典挂载的独立派生义项.

The 2012 attachment sets the boundary. The 2017 attachment supplies fields,
matched by content, never by the printed numbers in different editions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from html import escape
from html.parser import HTMLParser
import re
from typing import Any, Iterator

from backend.modules.senses import collins


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)

    def has(self, name: str) -> bool:
        return name in self.attrs.get("class", "").split()

    def walk(self) -> Iterator[Node]:
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(
            child.text() if isinstance(child, Node) else child
            for child in self.children)).strip()

    def html(self) -> str:
        attrs = "".join(f' {k}="{escape(v, quote=True)}"' for k, v in self.attrs.items())
        body = "".join(c.html() if isinstance(c, Node) else escape(c) for c in self.children)
        return f"<{self.tag}{attrs}>{body}</{self.tag}>"


class Tree(HTMLParser):
    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in {"br", "meta", "link", "img", "hr", "input"}:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def examples(node: Node, edition: int) -> list[tuple[str, str]]:
    pairs = []
    for child in node.walk():
        if (edition == 2012 and child.tag == "li") or (edition == 2017 and child.has("examples")):
            ps = [n.text() for n in child.children if isinstance(n, Node) and n.tag == "p"]
            if ps and ps[0]:
                pairs.append((ps[0], ps[1] if len(ps) > 1 else ""))
    return pairs


@dataclass
class Candidate:
    headword: str
    parent: str
    parent_block: int
    section: int
    parent_2012: dict[str, Any]
    runon_2012: dict[str, Any]
    parent_2017: dict[str, Any]
    runon_2017: dict[str, Any]
    match_basis: str

    @property
    def source_key(self) -> str:
        return f"collins-cobuild-2012:{self.parent}:{self.parent_block}:runon:{self.section}"

    def payload(self) -> dict[str, Any]:
        return {"source_key": self.source_key, **asdict(self)}


def _2017_blocks(html: str) -> list[dict[str, Any]]:
    out = []
    parent = None
    for node in Tree(html).root.walk():
        if node.tag != "li":
            continue
        majors = [n for n in node.walk() if n.has("collinsMajorTrans")]
        if len(majors) != 1:
            continue
        ps = [n for n in majors[0].walk() if n.tag == "p"]
        if not ps:
            continue
        p = ps[0]
        markers = [n for n in p.walk() if n.tag == "span" and n.attrs.get("title")]
        marker = markers[0] if markers else None
        prefix = []
        tail = []
        passed = False
        for child in p.children:
            if child is marker:
                passed = True
                continue
            text = child.text() if isinstance(child, Node) else child
            (tail if passed else prefix).append(text)
        before = re.sub(r"\s+", " ", "".join(prefix)).strip()
        # An attached run-on prints the word BEFORE its grammatical marker.
        derived_word = before.lower() if re.fullmatch(r"[A-Za-z]+(?:-[A-Za-z]+)*", before) else None
        text = re.sub(r"\s+", " ", "".join(tail)).strip() if marker else p.text()
        chinese = re.search(r"[（(]?\s*[\u3400-\u9fff].*", text)
        gloss = chinese.group(0).strip() if chinese else ""
        gloss = re.sub(r"\s*\[[^\]]*\]\s*$", "", gloss)
        concept = text[:chinese.start()].strip() if chinese else text
        block = {"block": len(out), "word": derived_word,
                 "pos": marker.text() if marker else "",
                 "pos_zh": marker.attrs.get("title", "") if marker else "",
                 "gloss_zh": collins.normalise_gloss(gloss), "concept_en": concept,
                 "examples": examples(node, 2017), "html": node.html(),
                 "parent": parent}
        out.append(block)
        if not derived_word:
            parent = block["block"]
    return out


def scan_entry(parent: str, html12: str, html17: str | None,
               words: set[str] | None = None) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """Resolve every run-on, returning unresolved cases instead of guessing."""
    result: list[Candidate] = []
    issues: list[dict[str, Any]] = []
    blocks17 = _2017_blocks(html17) if html17 else []
    parent12 = None
    parent_index = -1
    section = 0
    for node in Tree(html12).root.walk():
        if node.has("collins_en_cn"):
            parent_index += 1
            section = 0
            try:
                parsed = collins.parse_entry(parent, node.html())
                parent12 = (asdict(parsed[0]) if len(parsed) == 1
                            and parsed[0].kind is collins.Kind.WORD else None)
                if parent12:
                    parent12.pop("kind")
                    parent12["block_index"] = parent_index
                    parent12["examples"] = examples(node, 2012)
                    parent12["html"] = node.html()
            except collins.UnknownMarker as exc:
                parent12 = None
                issues.append({"parent": parent, "reason": "unknown_parent_marker", "detail": str(exc)})
        elif node.has("vExplain_r"):
            current_section = section
            section += 1
            titles = [c for c in node.children if isinstance(c, Node) and c.tag == "b"]
            word = titles[0].text().lower() if titles else ""
            word = re.sub(r"(?<=[a-z])-\s+(?=[a-z])", "", word)
            if words is not None and word not in words:
                continue
            base = {"parent": parent, "headword": word, "parent_block": parent_index,
                    "section": current_section}
            def reject(reason: str) -> None:
                issues.append({**base, "reason": reason, "html": node.html()})
            if not re.fullmatch(r"[a-z]+(?:-[a-z]+)*", word):
                reject("missing_runon_heading" if not word else
                       "affix_runon" if word.startswith("-") else
                       "phrase_runon" if " " in word else "nonword_runon")
                continue
            if word == parent:
                reject("same_word_usage")
                continue
            if not parent12 or not parent12.get("concept_en") or not parent12.get("gloss_zh"):
                reject("missing_or_unknown_parent")
                continue
            if not html17:
                reject("missing_2017_entry")
                continue
            en12 = {normalise(en) for en, _ in parent12["examples"]}
            matched_parents = [b for b in blocks17 if not b["word"] and (
                normalise(b["concept_en"]) == normalise(parent12["concept_en"])
                or bool(en12 & {normalise(en) for en, _ in b["examples"]}))]
            runon_examples = examples(node, 2012)
            en_runon = {normalise(en) for en, _ in runon_examples}
            attached = [b for b in blocks17 if b["word"] == word
                        and b["parent"] in {p["block"] for p in matched_parents}]
            by_example = [b for b in blocks17 if b["word"] == word and
                          en_runon & {normalise(en) for en, _ in b["examples"]}]
            if len(matched_parents) == 1 and len(attached) == 1:
                matched = attached[0]
                basis = "parent_definition_or_example_and_unique_attachment"
            elif len(by_example) == 1 and by_example[0]["parent"] is not None:
                matched = by_example[0]
                basis = "identical_runon_example"
                # Example identity is insufficient if parent evidence contradicts it.
                if matched_parents and matched["parent"] not in {p["block"] for p in matched_parents}:
                    reject("conflicting_parent_evidence")
                    continue
            else:
                reject("ambiguous_2017_match" if attached or by_example or len(matched_parents) > 1
                       else "no_content_match_2017")
                continue
            if not matched["pos"] or not matched["pos_zh"] or not matched["gloss_zh"]:
                reject("missing_2017_fields")
                continue
            if collins.PHRASE_MARKER.search(matched["pos"]):
                reject("phrase_runon")
                continue
            if matched["pos"].split("-")[0].split()[0].upper() not in collins.WORD_ROOTS:
                reject("unknown_2017_marker")
                continue
            p17 = blocks17[matched["parent"]]
            result.append(Candidate(word, parent, parent_index, current_section,
                                    parent12, {"examples": runon_examples, "html": node.html()},
                                    p17, matched, basis))
    return result, issues
