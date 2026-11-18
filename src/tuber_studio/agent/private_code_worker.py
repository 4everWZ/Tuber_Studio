"""QuickJS subprocess protocol. Input files exist only as strings in a fresh JS heap."""

import json
import sys

import quickjs


def main():
    job = json.load(sys.stdin.buffer)
    context = quickjs.Context()
    context.set_memory_limit(24 * 1024 * 1024)
    context.set_max_stack_size(256 * 1024)
    context.set_time_limit(1)
    context.set("files", context.parse_json(json.dumps(job["files"], ensure_ascii=True)))
    try:
        value = context.eval("(function(files){'use strict';\n" + job["script"] + "\n})(files)")
        if not isinstance(value, str) or not value or len(value) > 16000 or "\0" in value:
            raise ValueError
        # Reject isolated surrogate code units before storing or serving UTF-8.
        value.encode("utf-8")
        result = {"text": value}
    except (quickjs.JSException, ValueError, OverflowError):
        result = {"error": "脚本未返回有效文本，或已超过运行资源限制。"}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
