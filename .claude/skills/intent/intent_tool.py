#!/usr/bin/env python3
"""intent 문서 도구. 표준 라이브러리만 쓰며 macOS·Windows 에서 같게 동작한다.

    python intent_tool.py check <파일>   양식과 style.md 의 [검사] 규칙 검사. 위반이 있으면 종료 코드 1
    python intent_tool.py status         main 의 intent 목록과 열린 intent PR 목록

저장소 루트(docs/prd.md 가 있는 곳)에서 실행한다.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SECTIONS = [
    "Problem",
    "Proposed outcome",
    "Affected users and systems",
    "Constraints",
    "Open questions",
]
GUARANTEE_LABELS = {"보장하는 것", "보장하지 않는 것"}
DESCRIPTIVE_LIMIT = 15  # style.md 4-2
CONSTRAINT_LIMIT = 12  # 4-3
PARAGRAPH_LIMIT = 6  # 5-2
BODY_LIMIT = 600  # 5-4
TERM_LIMIT = 5  # 1-5

REQ_ID = r"N?FR-\d+[A-Z]?"
GH_ID = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
HEADER_RE = re.compile(
    rf"^Author: @({GH_ID}) \(([^()]+)\)\. "
    rf"Requirements: ({REQ_ID}(?:, {REQ_ID})*)\.$"
)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(.*)$")
LABEL_RE = re.compile(r"^\s*\*\*([^*]+)\*\*\s*$")
PAREN_RE = re.compile(r"\(([^()]*)\)")
CITE_START_RE = re.compile(r"^(?:PRD|architecture) |^shared:")
CITE_ITEM_RE = re.compile(
    r"^(PRD|architecture) (§\d+(?:·§\d+)*|부록 A)$"
    r"|^shared: ([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)$"
)
OWNER_ITEM_RE = re.compile(rf"^\S.* @{GH_ID}$|^\S.* 담당 미정$")
QUESTION_RE = re.compile(r"\?\s*\(([^()]+)\)\s*$")
TERM_RE = re.compile(r"(\S+?)\(([^()]+)\)")  # 용어 바로 뒤에 붙인 괄호 풀이
CODE_SPAN_RE = re.compile(r"`([^`]+)`")
CODE_NAME_RE = re.compile(r"[a-z][A-Z]|[A-Z]\w*\.[a-z_]")
LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
PLACEHOLDER_RE = re.compile(r"<[^<>\n]*[가-힣][^<>\n]*>")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.?!])\s+")
WORD_START = r"(?<![가-힣])"  # '대상이' 의 '상이' 같은 단어 중간 일치를 막는다
BANNED = [  # (패턴, 규칙, 바꿔 쓸 말)
    (WORD_START + r"(?:기기|디바이스)", "1-1", "장치"),
    (WORD_START + r"수행", "1-3", "한다"),
    (WORD_START + r"진행(?:하|한)", "1-3", "한다 또는 구체적인 동사"),
    (WORD_START + r"상이", "1-3", "다르다"),
    (WORD_START + r"기인", "1-3", "~때문이다"),
    (WORD_START + r"활용", "1-3", "쓴다"),
    (WORD_START + r"존재(?:하|한)", "1-3", "있다"),
    (WORD_START + r"상기 ", "1-3", "위"),
    (WORD_START + r"해당 ", "1-3", "대상 이름"),
    (r"에 대하여|에 대해", "1-3", "~를, ~에"),
    (r"[을를] 통해", "1-3", "~로"),
    (r"하도록 한다", "1-3", "~한다"),
]


# ── 저장소 문서 ──────────────────────────────────────────────────────


def find_root() -> Path | None:
    for d in [Path.cwd(), *Path.cwd().parents]:
        if (d / "docs" / "prd.md").is_file():
            return d
    return None


class Sources:
    """출처와 요구사항 ID가 실제로 있는지 확인한다."""

    def __init__(self, root: Path) -> None:
        prd = (root / "docs" / "prd.md").read_text(encoding="utf-8")
        arch = (root / "docs" / "architecture.md").read_text(encoding="utf-8")
        self.sections = {
            "PRD": set(re.findall(r"^## (\d+)\.", prd, re.M)),
            "architecture": set(re.findall(r"^## (\d+)\.", arch, re.M)),
        }
        self.prd_ids = set(re.findall(rf"^\| ({REQ_ID}) \|", prd, re.M))
        self.shared = "\n".join(
            p.read_text(encoding="utf-8")
            for p in sorted((root / "server" / "src" / "nearpilot" / "shared").glob("*.py"))
        )

    def cite_error(self, item: str) -> str | None:
        m = CITE_ITEM_RE.match(item)
        if not m:
            return f"출처 형식이 틀렸다: '{item}'"
        doc, where, name = m.groups()
        if name:
            missing = [p for p in name.split(".") if not re.search(rf"\b{re.escape(p)}\b", self.shared)]
            return f"shared 에 없는 이름이다: '{name}'" if missing else None
        if where == "부록 A":
            return None if doc == "PRD" else f"architecture 에는 부록 A 가 없다: '{item}'"
        missing = [n for n in re.findall(r"§(\d+)", where) if n not in self.sections[doc]]
        return f"{doc} 에 없는 절이다: '{item}'" if missing else None


# ── check ───────────────────────────────────────────────────────────


def is_meta_paren(inner: str) -> bool:
    """출처 괄호나 담당자 괄호인가."""
    return bool(CITE_START_RE.match(inner)) or "@" in inner or inner.strip().endswith("담당 미정")


def strip_for_count(text: str) -> str:
    """어절을 세기 전에 출처·담당자 괄호, 링크 주소, 강조 기호를 지운다."""
    text = PAREN_RE.sub(lambda m: "" if is_meta_paren(m.group(1)) else m.group(0), text)
    text = LINK_RE.sub(r"\1", text)
    text = text.replace("**", "").replace("__", "")
    return re.sub(r"\s+([.?!,])", r"\1", text).strip()


def sentences(text: str) -> list[str]:
    return [s for s in SENTENCE_SPLIT_RE.split(strip_for_count(text)) if s]


def check(path: Path) -> int:
    lines = path.read_text(encoding="utf-8").splitlines()
    errs: list[tuple[int, str, str]] = []

    def err(n: int, rule: str, msg: str) -> None:
        errs.append((n, rule, msg))

    root = find_root()
    src = Sources(root) if root else None
    if not src:
        print("경고: docs/prd.md 를 찾지 못해 출처·요구사항 ID 존재 검사는 건너뛴다.", file=sys.stderr)

    # 제목과 헤더
    if not lines or not re.match(r"^# Intent: \S", lines[0]):
        err(1, "양식", "첫 줄은 '# Intent: <제목>' 이어야 한다")
    header_no = next((i for i in range(1, len(lines)) if lines[i].strip()), None)
    if header_no is None:
        err(2, "양식", "헤더 줄이 없다")
    else:
        m = HEADER_RE.match(lines[header_no])
        if not m:
            err(header_no + 1, "양식",
                "헤더는 'Author: @<ID> (<역할>). Requirements: FR-xx, FR-yy.' 형식이어야 한다")
        elif src:
            for rid in m.group(3).split(", "):
                if rid not in src.prd_ids:
                    err(header_no + 1, "근거", f"PRD 부록 A 에 없는 요구사항 ID 다: {rid}")

    # 템플릿 흔적
    for n, line in enumerate(lines, 1):
        if "<!--" in line:
            err(n, "양식", "템플릿 주석이 남아 있다")
        elif PLACEHOLDER_RE.search(line):
            err(n, "양식", f"템플릿 자리표시자가 남아 있다: {PLACEHOLDER_RE.search(line).group(0)}")

    # 본문
    seen_sections: list[str] = []
    terms: dict[str, int] = {}
    body_words = 0
    section = label = None
    in_fence = in_comment = False
    para: list[tuple[int, str]] = []

    def check_words(n: int, text: str) -> None:
        """1-1·1-3 금지어와 1-5 용어 풀이. 표의 앞 칸에도 적용한다."""
        plain = PAREN_RE.sub(lambda m: "" if is_meta_paren(m.group(1)) else m.group(0), text)
        for pattern, rule, instead in BANNED:
            for hit in re.findall(pattern, plain):
                err(n, rule, f"쓰지 않는 말: '{hit.strip()}' → {instead}")
        # `OK!(command_id)` 처럼 백틱 안에 있는 괄호는 용어 풀이가 아니다
        masked = CODE_SPAN_RE.sub(lambda m: m.group(0).replace("(", "<").replace(")", ">"), plain)
        for term, inner in TERM_RE.findall(masked):
            if not is_meta_paren(inner):
                terms.setdefault(term.strip("`*"), n)

    def check_unit(n: int, text: str, need_cite: bool) -> None:
        nonlocal body_words
        check_words(n, text)
        for span in CODE_SPAN_RE.findall(text):
            if CODE_NAME_RE.search(span):
                err(n, "1-4", f"코드상 이름은 '주고받는 것' 표의 마지막 열에만 쓴다: `{span}`")
        cites = [c for c in PAREN_RE.findall(text) if CITE_START_RE.match(c)]
        if src:
            for c in cites:
                for item in c.split(", "):
                    e = src.cite_error(item.strip())
                    if e:
                        err(n, "6-1", e)
        if need_cite and not cites:
            err(n, "6-2", "이 항목에는 출처가 있어야 한다")
        limit = CONSTRAINT_LIMIT if section == "Constraints" else DESCRIPTIVE_LIMIT
        for s in sentences(text):
            count = len(s.split())
            body_words += count
            if count > limit:
                rule = "4-3" if limit == CONSTRAINT_LIMIT else "4-2"
                err(n, rule, f"문장이 {count}어절이다 (최대 {limit}): {s[:40]}…")

    def check_question(n: int, text: str) -> None:
        if text.strip() == "없음.":
            return
        q = QUESTION_RE.search(text)
        owners = q.group(1).split(", ") if q else []
        if not q or not all(OWNER_ITEM_RE.match(o.strip()) for o in owners):
            err(n, "6-3", "Open questions 항목은 '<질문>? (<역할> @<ID>)' 형식이어야 한다")

    def flush() -> None:
        if not para:
            return
        n = para[0][0]
        text = " ".join(t for _, t in para)
        check_unit(n, text, need_cite=False)
        count = len(sentences(text))
        if count > PARAGRAPH_LIMIT:
            err(n, "5-2", f"문단이 {count}문장이다 (최대 {PARAGRAPH_LIMIT})")
        para.clear()

    for n, raw in enumerate(lines, 1):
        line = raw.rstrip()
        if n == 1 or (header_no is not None and n == header_no + 1):
            continue
        if line.lstrip().startswith("```"):
            flush()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if in_comment or line.lstrip().startswith("<!--"):
            in_comment = "-->" not in line
            continue
        h = HEADING_RE.match(line)
        if h:
            flush()
            level, title = len(h.group(1)), h.group(2)
            if level == 2 and title in SECTIONS:
                seen_sections.append(title)
                section, label = title, None
            else:
                err(n, "양식", f"정해진 섹션이 아닌 제목이다: {line}")
            continue
        if not line.strip():
            flush()
            continue
        if line.lstrip().startswith("|"):
            flush()
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not all(set(c) <= set("-: ") for c in cells):
                check_words(n, " | ".join(cells[:-1]))
            continue
        lb = LABEL_RE.match(line)
        if lb:
            flush()
            label = lb.group(1).strip()
            continue
        li = LIST_RE.match(line)
        if li:
            flush()
            text = li.group(1)
            need = section == "Constraints" or (section == "Affected users and systems" and label in GUARANTEE_LABELS)
            check_unit(n, text, need_cite=need)
            if section == "Open questions":
                check_question(n, text)
            continue
        para.append((n, line.strip()))
    flush()

    if seen_sections != SECTIONS:
        err(0, "양식", f"섹션은 {SECTIONS} 순서로 한 번씩 있어야 한다. 지금: {seen_sections}")
    if len(terms) > TERM_LIMIT:
        err(0, "1-5", f"괄호로 풀이한 용어가 {len(terms)}개다 (최대 {TERM_LIMIT}): {', '.join(terms)}")
    if body_words > BODY_LIMIT:
        err(0, "5-4", f"본문이 {body_words}어절이다 (최대 {BODY_LIMIT})")

    for n, rule, msg in sorted(errs):
        print(f"{path}:{n}: [{rule}] {msg}")
    print(f"본문 {body_words}어절, 풀이한 용어 {len(terms)}개")
    print(f"위반 {len(errs)}개" if errs else "위반 없음")
    return 1 if errs else 0


# ── status ──────────────────────────────────────────────────────────


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def status(ref: str) -> int:
    print(f"## main 의 intent ({ref})")
    ls = run(["git", "ls-tree", "--name-only", ref, "docs/intent/"])
    if ls.returncode != 0:
        print(f"{ref} 를 읽지 못했다. 'git fetch origin' 을 먼저 실행한다.\n{ls.stderr.strip()}")
        return 1
    paths = [p for p in ls.stdout.splitlines() if p.endswith(".md") and p.count("/") == 2]
    for p in paths:
        # 본문은 출력하지 않는다. 제목과 헤더 줄만 쓴다.
        body = run(["git", "show", f"{ref}:{p}"]).stdout.splitlines()
        title = re.sub(r"^# Intent: ", "", body[0]) if body else ""
        m = next((HEADER_RE.match(x) for x in body[1:6] if x.startswith("Author:")), None)
        who = f"@{m.group(1)} ({m.group(2)})" if m else "헤더 형식 아님"
        req = m.group(3) if m else "-"
        print(f"- {Path(p).stem} | {title} | {who} | {req}")
    if not paths:
        print("- 없음")

    print("\n## 열린 intent PR")
    try:
        pr = run(["gh", "pr", "list", "--state", "open", "--limit", "200",
                  "--json", "number,title,headRefName,author,url"])
    except FileNotFoundError:
        print("gh 가 없어 열린 PR 을 확인하지 못했다.")
        return 0
    if pr.returncode != 0:
        print(f"gh 로 열린 PR 을 확인하지 못했다: {pr.stderr.strip()}")
        return 0
    prs = [x for x in json.loads(pr.stdout or "[]") if x["headRefName"].startswith("intent/")]
    for x in prs:
        print(f"- #{x['number']} {x['title']} | @{x['author']['login']} | {x['headRefName']} | {x['url']}")
    if not prs:
        print("- 없음")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # Windows 콘솔(cp949)에서 한글이 깨지지 않게
        except AttributeError:
            pass
    ap = argparse.ArgumentParser(description="intent 문서 도구")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="양식과 style.md 의 [검사] 규칙 검사")
    c.add_argument("file", type=Path)
    s = sub.add_parser("status", help="main 의 intent 와 열린 intent PR 목록")
    s.add_argument("--ref", default="origin/main")
    a = ap.parse_args()
    return check(a.file) if a.cmd == "check" else status(a.ref)


if __name__ == "__main__":
    sys.exit(main())
