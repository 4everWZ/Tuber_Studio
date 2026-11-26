"""Deep public research (GitHub repos, web pages, allowlisted workspace docs) and local UI screenshots."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ...core.validation import linked
from ...infra.web_client.fetch import fetch_excerpt, public_https_address
from . import remotion_assets as _ra

_GITHUB_REPO_PATH_RE = re.compile(r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/.*)?$")
_FORBIDDEN_WORKSPACE_PARTS = frozenset({".env", "config", "runtime", "credentials", ".git", "__pycache__"})
_MAX_EXCERPT_CHARS = 4000
_MAX_DOC_SNIPPET = 1200


def _sanitize(text: str) -> str:
    return _ra._sanitize_diagnostic(text)


def _extract_github_owner_repo(url: httpx.URL) -> tuple[str, str] | None:
    if (url.host or "").lower() != "github.com":
        return None
    match = _GITHUB_REPO_PATH_RE.fullmatch(url.path or "")
    if not match:
        return None
    owner, repo = match.group(1), match.group(2)
    if owner in {"topics", "explore", "settings", "orgs", "marketplace", "search"}:
        return None
    return owner, repo


def _extract_facts_from_text(text: str, *, limit: int = 8) -> list[str]:
    facts: list[str] = []
    for raw_line in re.split(r"[\r\n]+", str(text or "")):
        clean = re.sub(r"^[#>*`\-\d.\s]+", "", raw_line).strip()
        if 8 <= len(clean) <= 180 and clean not in facts:
            facts.append(clean)
            if len(facts) >= limit:
                break
    return facts


async def _safe_https_get_json_or_text(
    client: httpx.AsyncClient,
    url_str: str,
    *,
    accept: str,
    as_json: bool = False,
    max_bytes: int = 256 * 1024,
) -> tuple[int, Any]:
    parsed = _ra.parse_public_https_url(url_str)
    resolve_addr = getattr(_ra, "public_https_address", public_https_address)
    for _ in range(3):
        host, address = await resolve_addr(parsed)
        target = parsed.copy_with(host=address)
        client.cookies.clear()
        async with client.stream(
            "GET",
            target,
            headers={
                "Host": f"[{host}]" if ":" in host else host,
                "Accept": accept,
                "Accept-Encoding": "gzip, deflate, identity",
                "User-Agent": "TuberStudio-RemotionResearch/1.0",
            },
            extensions={"sni_hostname": host},
        ) as resp:
            if resp.status_code in {301, 302, 303, 307, 308} and (loc := resp.headers.get("location")):
                parsed = _ra.parse_public_https_url(str(parsed.join(loc)))
                continue
            if resp.status_code >= 400:
                return resp.status_code, None
            raw = bytearray()
            async for chunk in resp.aiter_bytes(chunk_size=8192):
                remaining = max_bytes - len(raw)
                raw.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    break
            text = raw.decode(resp.encoding or "utf-8", errors="replace")
            if as_json:
                try:
                    return resp.status_code, json.loads(text)
                except ValueError:
                    return resp.status_code, None
            return resp.status_code, text
    return 310, None


async def _fetch_github_deep(owner: str, repo: str) -> dict[str, Any]:
    repo_url = f"https://github.com/{owner}/{repo}"
    overview_lines: list[str] = []
    modules: list[str] = []
    docs_excerpts: list[str] = []
    readme_text = ""

    async with (
        asyncio.timeout(10.0),
        httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        ) as client,
    ):
        meta_code, meta = await _safe_https_get_json_or_text(
            client,
            f"https://api.github.com/repos/{owner}/{repo}",
            accept="application/vnd.github+json",
            as_json=True,
        )
        if meta_code == 200 and isinstance(meta, dict):
            desc = str(meta.get("description") or "").strip()
            lang = str(meta.get("language") or "").strip()
            stars = meta.get("stargazers_count")
            topics = [str(t) for t in (meta.get("topics") or ())[:6]]
            overview_lines.append(
                f"{owner}/{repo}"
                + (f" ({lang})" if lang else "")
                + (f" ★{stars}" if isinstance(stars, int) else "")
                + (f"：{desc}" if desc else "")
            )
            if topics:
                overview_lines.append("标签：" + ", ".join(topics))

        rd_code, rd_body = await _safe_https_get_json_or_text(
            client,
            f"https://api.github.com/repos/{owner}/{repo}/readme",
            accept="application/vnd.github.raw+json",
        )
        if rd_code == 200 and isinstance(rd_body, str) and rd_body.strip():
            readme_text = rd_body.strip()[:_MAX_EXCERPT_CHARS]

        dir_code, entries = await _safe_https_get_json_or_text(
            client,
            f"https://api.github.com/repos/{owner}/{repo}/contents",
            accept="application/vnd.github+json",
            as_json=True,
        )
        doc_candidates: list[str] = []
        has_docs_dir = False
        if dir_code == 200 and isinstance(entries, list):
            for entry in entries[:30]:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "").strip()
                etype = str(entry.get("type") or "")
                if not name:
                    continue
                if etype == "dir":
                    modules.append(name)
                    if name.lower() == "docs":
                        has_docs_dir = True
                elif name.lower().endswith(".md") and name.lower() != "readme.md":
                    doc_candidates.append(str(entry.get("path") or name))
        if has_docs_dir and len(doc_candidates) < 3:
            d_code, d_entries = await _safe_https_get_json_or_text(
                client,
                f"https://api.github.com/repos/{owner}/{repo}/contents/docs",
                accept="application/vnd.github+json",
                as_json=True,
            )
            if d_code == 200 and isinstance(d_entries, list):
                for entry in d_entries[:15]:
                    if isinstance(entry, dict) and str(entry.get("name") or "").lower().endswith(".md"):
                        doc_candidates.append(str(entry.get("path") or ""))
                        if len(doc_candidates) >= 3:
                            break
        for doc_path in doc_candidates[:3]:
            if not doc_path:
                continue
            c_code, c_text = await _safe_https_get_json_or_text(
                client,
                f"https://api.github.com/repos/{owner}/{repo}/contents/{doc_path.lstrip('/')}",
                accept="application/vnd.github.raw+json",
            )
            if c_code == 200 and isinstance(c_text, str) and c_text.strip():
                docs_excerpts.append(f"[{doc_path}]\n{c_text.strip()[:_MAX_DOC_SNIPPET]}")

    parts = [*overview_lines]
    if modules:
        parts.append("顶层目录：" + ", ".join(modules[:12]))
    if readme_text:
        parts.append("README:\n" + readme_text)
    if docs_excerpts:
        parts.extend(docs_excerpts)
    combined = "\n\n".join(p for p in parts if p).strip()
    if not combined:
        raise ValueError("GitHub 仓库未返回可读公开内容")
    return {
        "url": repo_url,
        "title": f"{owner}/{repo} 开源仓库"[:160],
        "excerpt": combined[:_MAX_EXCERPT_CHARS],
        "modules": modules[:16],
        "truncated": len(combined) > _MAX_EXCERPT_CHARS,
    }


def _collect_allowlisted_workspace_docs(workspace_root: Path | None) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Read ONLY allowlisted public repo docs (README.md, AGENTS.md, docs/**/*.md, package names)."""
    root = (Path(workspace_root) if workspace_root is not None else Path(__file__).resolve().parents[4]).resolve()
    if not root.is_dir() or linked(root):
        return [], [], []

    pages: list[dict[str, Any]] = []
    facts: list[str] = []
    modules: list[str] = []

    def _safe_read_md(path: Path, label: str) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if not resolved.is_relative_to(root) or linked(path) or linked(resolved) or not resolved.is_file():
            return
        rel = resolved.relative_to(root).as_posix()
        if any(part in _FORBIDDEN_WORKSPACE_PARTS for part in Path(rel).parts):
            return
        try:
            raw = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        headings = [line.strip() for line in raw.splitlines() if line.lstrip().startswith("#")][:8]
        summary = " ".join(
            line.strip() for line in raw.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )[:900]
        excerpt = "\n".join([*headings[:5], summary]).strip()[:_MAX_DOC_SNIPPET]
        if excerpt:
            pages.append({
                "url": "",
                "title": f"工作区文档：{label}",
                "excerpt": excerpt,
                "fetch_status": "workspace_doc",
                "rel_path": rel,
            })
            facts.extend(_extract_facts_from_text(excerpt, limit=3))

    for fixed_name in ("README.md", "AGENTS.md"):
        _safe_read_md(root / fixed_name, fixed_name)

    docs_dir = root / "docs"
    if docs_dir.is_dir() and not linked(docs_dir):
        md_files = sorted(p for p in docs_dir.rglob("*.md") if not linked(p))
        for doc_file in md_files[:10]:
            rel = doc_file.relative_to(root).as_posix()
            _safe_read_md(doc_file, rel)

    pkg_dir = root / "src" / "tuber_studio"
    if pkg_dir.is_dir() and not linked(pkg_dir):
        for entry in sorted(pkg_dir.iterdir(), key=lambda p: p.name):
            if entry.name.startswith((".", "_")) or entry.name in _FORBIDDEN_WORKSPACE_PARTS or linked(entry):
                continue
            if entry.is_dir():
                modules.append(f"tuber_studio.{entry.name}")
                for sub in sorted(entry.iterdir(), key=lambda p: p.name):
                    if sub.is_dir() and not sub.name.startswith((".", "_")) and not linked(sub):
                        modules.append(f"tuber_studio.{entry.name}.{sub.name}")

    return pages, list(dict.fromkeys(facts))[:16], list(dict.fromkeys(modules))[:20]


def _format_watched_digest(candidates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    digest: list[dict[str, Any]] = []
    for item in candidates[:8]:
        if not isinstance(item, dict):
            continue
        bvid = str(item.get("bvid") or "").strip()
        if not bvid:
            continue
        title = str(item.get("title") or bvid).strip()[:160]
        summary = " ".join(str(item.get("summary") or item.get("description") or "").split())[:320]
        author = str(item.get("author") or item.get("owner_name") or "").strip()[:80]
        tags = [str(t)[:32] for t in (item.get("tags") or ())[:6] if str(t).strip()]
        key_points = _extract_facts_from_text(summary, limit=3) or ([title] if title else [])
        digest.append({
            "bvid": bvid,
            "title": title,
            "url": str(item.get("url") or f"https://www.bilibili.com/video/{bvid}"),
            "author": author,
            "summary": summary,
            "tags": tags,
            "key_points": key_points,
        })
    return digest


async def fetch_deep_reference_materials(
    urls: Sequence[str],
    *,
    fetch_fn=fetch_excerpt,
    include_workspace_docs: bool = False,
    workspace_root: Path | None = None,
    topic: str = "",
    video_mode: str = "commentary",
    candidates: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Fetch bounded deep research from public URLs, GitHub repos, allowlisted docs, and watched videos."""
    resolve_addr = getattr(_ra, "public_https_address", public_https_address)
    validated: list[tuple[str, httpx.URL, str | None]] = []
    for raw in urls:
        parsed = _ra.parse_public_https_url(raw)
        clean = str(parsed)
        dns_err: str | None = None
        try:
            await resolve_addr(parsed)
        except ValueError:
            raise
        except OSError as exc:
            if fetch_fn is fetch_excerpt:
                dns_err = _sanitize(f"{type(exc).__name__}: {exc}")[:160] or "DNS 解析失败"
        validated.append((clean, parsed, dns_err))

    sources: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    facts: list[str] = []
    modules: list[str] = []
    diagnostics: list[str] = []

    for clean, parsed, dns_err in validated:
        host_or_url = (parsed.host or clean)[:160]
        src_id = f"web:{hashlib.sha256(clean.encode('utf-8')).hexdigest()[:12]}"
        if dns_err is not None:
            sources.append({
                "source_id": src_id, "kind": "web_research", "scope": "public",
                "title": host_or_url, "url": clean,
                "excerpt": "公开链接抓取失败，已保留原地址供参考。", "fetch_status": "fallback", "fetch_error": dns_err,
            })
            pages.append({
                "url": clean, "title": host_or_url,
                "excerpt": "公开链接抓取失败，已保留原地址供参考。", "fetch_status": "fallback", "fetch_error": dns_err,
            })
            diagnostics.append(f"{host_or_url}: {dns_err}")
            continue

        gh_repo = _extract_github_owner_repo(parsed)
        try:
            res: dict[str, Any] | None = None
            if gh_repo is not None and fetch_fn is fetch_excerpt:
                try:
                    res = await _fetch_github_deep(*gh_repo)
                except Exception:
                    res = None
            if res is None:
                raw_res = fetch_fn(clean)
                if inspect.isawaitable(raw_res):
                    raw_res = await raw_res
                res = raw_res if isinstance(raw_res, dict) else {}

            page_url = str(res.get("url") or clean).strip()
            if not page_url.startswith("https://"):
                page_url = clean
            title = str(res.get("title") or "").strip()[:160] or host_or_url
            raw_excerpt = str(res.get("excerpt") or "").strip()
            sources.append({
                "source_id": f"web:{hashlib.sha256(page_url.encode('utf-8')).hexdigest()[:12]}",
                "kind": "web_research", "scope": "public",
                "title": title, "url": page_url,
                "excerpt": raw_excerpt[:480] or title[:480],
                "fetch_status": "fetched",
            })
            pages.append({
                "url": page_url, "title": title,
                "excerpt": raw_excerpt[:_MAX_EXCERPT_CHARS],
                "truncated": bool(res.get("truncated")) or len(raw_excerpt) > _MAX_EXCERPT_CHARS,
                "fetch_status": "fetched",
            })
            facts.extend(_extract_facts_from_text(raw_excerpt, limit=6))
            for mod in res.get("modules") or ():
                if str(mod) not in modules:
                    modules.append(str(mod))
        except Exception as exc:
            err_msg = _sanitize(f"{type(exc).__name__}: {exc}")[:160] or "抓取失败"
            sources.append({
                "source_id": src_id, "kind": "web_research", "scope": "public",
                "title": host_or_url, "url": clean,
                "excerpt": "公开链接抓取失败，已保留原地址供参考。", "fetch_status": "fallback", "fetch_error": err_msg,
            })
            pages.append({
                "url": clean, "title": host_or_url,
                "excerpt": "公开链接抓取失败，已保留原地址供参考。", "fetch_status": "fallback", "fetch_error": err_msg,
            })
            diagnostics.append(f"{host_or_url}: {err_msg}")

    if include_workspace_docs:
        ws_pages, ws_facts, ws_modules = _collect_allowlisted_workspace_docs(workspace_root)
        pages.extend(ws_pages)
        facts.extend(ws_facts)
        for mod in ws_modules:
            if mod not in modules:
                modules.append(mod)
        if ws_pages or ws_modules:
            summary_txt = "；".join(p["title"] for p in ws_pages[:3])
            if ws_modules:
                summary_txt = f"{summary_txt}（模块：{', '.join(ws_modules[:6])}）".strip("；")
            sources.append({
                "source_id": f"workspace:{hashlib.sha256((topic or 'docs').encode('utf-8')).hexdigest()[:12]}",
                "kind": "operator_topic", "scope": "public",
                "title": "工坊公开文档与结构",
                "excerpt": summary_txt[:480] or "允许读取的公开仓库文档摘要",
                "fetch_status": "manual",
            })

    watched_digest = _format_watched_digest(candidates)
    if video_mode in {"watched_roundup", "commentary"} or not facts:
        for item in watched_digest[:5]:
            line = f"{item['title']}（{item['bvid']}）：{item['summary'][:120]}".strip("：")
            if line not in facts:
                facts.append(line)

    return {
        "sources": sources,
        "pages": pages,
        "facts": list(dict.fromkeys(facts))[:24],
        "modules": modules[:20],
        "watched_digest": watched_digest,
        "diagnostics": diagnostics,
    }


def _is_loopback_base_url(base_url: str) -> tuple[bool, str]:
    try:
        parsed = urlsplit(str(base_url or "").strip())
    except ValueError:
        return False, "本地工作台地址格式无效"
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in {"127.0.0.1", "localhost", "::1"}:
        return False, "仅允许截取本机回环地址 (127.0.0.1 / localhost) 的工作台页面"
    return True, f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


async def capture_workbench_screenshots(
    output_dir: Path | str,
    *,
    base_url: str = "http://127.0.0.1:9020",
    routes: Sequence[str] | None = None,
    max_captures: int = 6,
) -> dict[str, Any]:
    """Capture 1920x1080 local Tuber Studio screenshots via Playwright Chromium (loopback-only)."""
    ok_url, normalized_base = _is_loopback_base_url(base_url)
    if not ok_url:
        return {"ok": False, "error": normalized_base, "captured": []}

    out_path = Path(output_dir)
    if linked(out_path):
        return {"ok": False, "error": "截图输出目录不能使用符号链接", "captured": []}

    try:
        from playwright.async_api import async_playwright
    except Exception:
        return {"ok": False, "error": "Playwright 未安装，无法截取本地工作台画面", "captured": []}

    limit = max(1, min(int(max_captures), 12))
    raw_routes = list(routes) if routes else ["/", "/operations", "/video-studio", "/persona", "/live", "/settings"]
    clean_routes: list[str] = []
    for raw in raw_routes:
        r = str(raw or "").strip()
        if not r:
            continue
        if not r.startswith("/"):
            r = "/" + r
        if r not in clean_routes:
            clean_routes.append(r)
        if len(clean_routes) >= limit:
            break

    out_path.mkdir(parents=True, exist_ok=True)
    captured: list[dict[str, Any]] = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1920, "height": 1080})
                for idx, route in enumerate(clean_routes, start=1):
                    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", route.strip("/") or "home").strip("_")[:32] or "page"
                    fname = f"ui_{idx:02d}_{slug}.png"
                    target = out_path / fname
                    await page.goto(f"{normalized_base}{route}", wait_until="domcontentloaded", timeout=10000)
                    await page.wait_for_timeout(450)
                    await page.screenshot(path=str(target), full_page=False)
                    captured.append({
                        "route": route,
                        "filename": fname,
                        "path": str(target),
                        "rel_path": f"uploads/{fname}",
                    })
            finally:
                await browser.close()
    except Exception as exc:
        return {
            "ok": False,
            "error": f"本地工作台截图失败: {_sanitize(f'{type(exc).__name__}: {exc}')}",
            "captured": captured,
        }

    return {"ok": True, "error": None, "captured": captured}
