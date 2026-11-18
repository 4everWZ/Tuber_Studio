"""Keep one offline Chromium page for the task owning this private pipe."""

import base64
import html
import json
import os
import sys
from html.parser import HTMLParser
from urllib.parse import unquote, urldefrag, urljoin

from playwright.sync_api import sync_playwright


class ReadingPage(HTMLParser):
    tags = set("main article section nav header footer aside div span h1 h2 h3 h4 h5 h6 p blockquote "
               "strong b em i small pre code ul ol li dl dt dd table thead tbody tr th td caption "
               "figure figcaption details summary br hr a label input fieldset legend".split())
    hidden = {"script", "template", "iframe", "object", "svg", "title"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip, self.count, self.style = [], [], 0, False

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden:
            self.skip.append(tag)
        if tag == "style" and not self.skip:
            self.style = True
            self.parts.append("<style>")
        if self.skip or tag not in self.tags:
            return
        self.count += 1
        if self.count > 5000:
            raise ValueError("文档元素过多")
        attrs = dict(attrs)
        kept = {k: v[:2048] for k, v in attrs.items() if k in {"id", "lang", "dir", "class", "style", "for"} and v}
        if tag == "input":
            if attrs.get("type") not in {"checkbox", "radio"}:
                return
            kept["type"] = attrs["type"]
            for key in ("name", "aria-label", "checked", "disabled"):
                if key in attrs:
                    kept[key] = (attrs[key] or "")[:160]
        if "hidden" in attrs:
            kept["hidden"] = ""
        if tag == "details" and "open" in attrs:
            kept["open"] = ""
        if tag == "a" and attrs.get("href"):
            kept["data-link"] = attrs["href"][:2048]
            kept["role"] = "link"
        encoded = "".join(f' {k}="{html.escape(v, quote=True)}"' for k, v in kept.items())
        self.parts.append(f"<{tag}{encoded}>")

    def handle_endtag(self, tag):
        if tag == "style" and self.style:
            self.parts.append("</style>")
            self.style = False
        elif self.skip:
            if self.skip[-1] == tag:
                self.skip.pop()
        elif tag in self.tags:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data if self.style else html.escape(data))


VIEW = """() => {
  const text = [], bounds = document.createRange();
  function visible(el) {
    if (!el.checkVisibility()) return false;
    for (let p=el;p;p=p.parentElement) {
      if (p.tagName==="DETAILS" && !p.open && !p.querySelector(":scope > summary")?.contains(el)) return false;
    }
    return true;
  }
  const nodes = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let length = 0;
  for (let n; (n = nodes.nextNode()) && length < 4000;) {
    if (!n.textContent.trim() || !visible(n.parentElement)) continue;
    bounds.selectNodeContents(n);
    const r = bounds.getBoundingClientRect();
    if (r.height && r.width && r.bottom > 0 && r.top < innerHeight) {
      const boundary = (before) => {
        let lo=0, hi=n.length;
        while(lo<hi) {
          const m=(lo+hi)>>>1;
          bounds.setStart(n,m); bounds.setEnd(n,m+1);
          if(before(bounds.getBoundingClientRect())) lo=m+1; else hi=m;
        }
        return lo;
      };
      const start=r.top<0 ? boundary(c=>c.bottom<=0) : 0;
      const end=r.bottom>innerHeight ? boundary(c=>c.top<innerHeight) : n.length;
      const part = n.textContent.slice(start,end).trim().slice(0, 4000 - length);
      text.push(part); length += part.length;
    }
  }
  return {text: text.join(" "), scroll: scrollY,
    opened: [...document.querySelectorAll("details")].flatMap((d,i) => d.open ? [i] : []),
    checked: [...document.querySelectorAll("input")].flatMap((e,i) => e.checked ? [i] : []),
    controls: [...document.querySelectorAll("summary,a[data-link],input")].flatMap((e,i) => {
      const r = e.getBoundingClientRect();
      if (!visible(e) || e.disabled || !r.width || !r.height || r.top >= innerHeight || r.bottom <= 0) return [];
      const label = e.labels?.[0]?.innerText || e.getAttribute("aria-label") || e.innerText || "";
      return [{index:i+1,label:label.trim().slice(0,120),href:e.dataset.link || null,
        kind:e.tagName==="SUMMARY"?"disclosure":e.tagName==="INPUT"?"choice":"link"}];
    }).slice(0,60)};
}"""


def markup(document):
    parser = ReadingPage()
    parser.feed(document)
    parser.close()
    return ("<!doctype html><meta charset=utf-8><meta http-equiv='Content-Security-Policy' "
              "content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
              "<style>body{font:18px/1.6 system-ui;margin:1em 2em;"
              "overflow-wrap:anywhere}a{color:#165991;text-decoration:underline}pre{white-space:pre-wrap}"
              "table{border-collapse:collapse}td,th{border:1px solid #888;padding:4px}</style><body>"
              + "".join(parser.parts))


def render(page, data, previous_url):
    state, action = data["state"], data["action"]
    if previous_url != data["url"] or action["kind"] == "open":
        page.set_content(markup(data["document"]), wait_until="domcontentloaded")
        if "opened" in state:
            page.locator("details").evaluate_all("(ds,open) => ds.forEach((d,i) => d.open=open.includes(i))",
                                                 state["opened"])
        if "checked" in state:
            page.locator("input").evaluate_all("(els,ids) => els.forEach((e,i) => e.checked=ids.includes(i))",
                                               state["checked"])
        page.evaluate("(y) => scrollTo(0,y)", state.get("scroll", 0))
    if action["kind"] == "activate":
        page.locator("summary,a[data-link],input").nth(action["index"] - 1).click()
    elif action["kind"] == "anchor":
        page.evaluate("(id) => id ? document.getElementById(id)?.scrollIntoView() : scrollTo(0,0)", action["value"])
    elif action["kind"] in {"next", "previous"}:
        page.evaluate("(direction) => scrollBy(0,direction*(innerHeight-64))", 1 if action["kind"] == "next" else -1)
    elif action["kind"] == "find":
        found = page.evaluate("""q => { const w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
            for(let n;n=w.nextNode();) if(n.textContent.toLocaleLowerCase().includes(q.toLocaleLowerCase())) {
                for(let p=n.parentElement;p;p=p.parentElement) if(p.tagName==="DETAILS") p.open=true;
                if(!n.parentElement.checkVisibility()) continue;
                n.parentElement.scrollIntoView({block:"center"}); return true; } return false; }""", action["value"])
        if not found:
            raise ValueError("页内没有找到该文字")
    view = page.evaluate(VIEW)
    controls = []
    for control in view.pop("controls"):
        link = control.pop("href")
        if link:
            try:
                url, fragment = urldefrag(urljoin(data["url"], link))
            except ValueError:
                continue
            if url not in data["sources"]:
                continue
            control.update(source=data["sources"].index(url) + 1, fragment=unquote(fragment)[:256])
        controls.append(control)
    image = page.screenshot(type="jpeg", quality=65, timeout=2500)
    if len(image) > 256 * 1024:
        raise ValueError("页面截图超过上限")
    return view | {"controls": controls, "image": base64.b64encode(image).decode("ascii")}


def serve():
    with sync_playwright() as pw:
        channel = os.environ.get("TUBER_READING_BROWSER_CHANNEL") or ("msedge" if sys.platform == "win32" else "chromium")
        if channel not in {"msedge", "chrome", "chromium"}:
            raise ValueError("不支持的 Chromium 安装渠道")
        browser = pw.chromium.launch(channel=channel, chromium_sandbox=True, args=["--disable-background-networking",
            "--disable-extensions", "--disable-sync", "--host-resolver-rules=MAP * ~NOTFOUND",
            "--proxy-server=http://unreachable.invalid:9", "--proxy-bypass-list=<-loopback>"])
        context = browser.new_context(offline=True, java_script_enabled=False, service_workers="block",
                                      accept_downloads=False, viewport={"width": 960, "height": 600})
        context.route("**/*", lambda route: route.abort())
        page = context.new_page()
        page.set_default_timeout(2000)
        cdp = context.new_cdp_session(page)
        page_id = cdp.send("Target.getTargetInfo")["targetInfo"]["targetId"]
        previous = None
        for line in sys.stdin:
            try:
                data = json.loads(line)
                result = render(page, data, previous) | {"page_id": page_id, "chromium_version": browser.version}
                previous = data["url"]
            except ValueError as exc:
                previous, result = None, {"error": str(exc)}
            except Exception:
                previous, result = None, {"error": "Chromium 未能读取当前页面"}
            print(json.dumps(result, ensure_ascii=True), flush=True)
        browser.close()


if __name__ == "__main__":
    serve()
