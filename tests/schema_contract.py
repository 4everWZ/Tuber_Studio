def validate_schema_complexity(schema):
    # Schemas can describe ordinary URLs as data, but cannot acquire a resolver
    # capable of reading URLs/files. Bound nesting before the native converter.
    pending = [(schema, 0)]
    visited = 0
    while pending:
        value, depth = pending.pop()
        visited += 1
        if depth > 32 or visited > 4096:
            raise ValueError("Response schema exceeds the supported complexity")
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef", "$id"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise ValueError("Response schemas support local references only")
                pending.append((child, depth + 1))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
