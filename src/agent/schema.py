"""The small part of JSON Schema the tools declare, built and checked in one place: what the
model is told to send is exactly what is accepted."""

_TYPES = {"string": str, "integer": int, "boolean": bool}


def text(description: str, max_length: int) -> dict:
    return {"type": "string", "description": description, "maxLength": max_length}


def integer(description: str, minimum: int, maximum: int) -> dict:
    return {"type": "integer", "description": description, "minimum": minimum, "maximum": maximum}


def arguments(optional: tuple[str, ...] = (), **properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": [name for name in properties if name not in optional],
        "additionalProperties": False,
    }


def problem_with(schema: dict, args: object) -> str | None:
    """Why `args` does not fit `schema`, in words the model can act on; None when it fits."""
    if not isinstance(args, dict):
        return "Arguments must be an object."
    properties = schema["properties"]
    unknown = sorted(str(name) for name in args if name not in properties)
    if unknown:
        return f"Unknown argument: {unknown[0][:40]}."
    for name in schema["required"]:
        if name not in args:
            return f"Missing argument: {name}."
    for name, value in args.items():
        problem = _problem_with_value(properties[name], value)
        if problem is not None:
            return f"Argument {name} {problem}."
    return None


def _problem_with_value(schema: dict, value: object) -> str | None:
    expected = _TYPES[schema["type"]]
    # bool is an int for Python, not for JSON.
    if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
        return f"must be of type {schema['type']}"
    if expected is str:
        if not value.strip():
            return "must not be empty"
        if len(value) > schema["maxLength"]:
            return f"must be at most {schema['maxLength']} characters"
    if expected is int and not schema["minimum"] <= value <= schema["maximum"]:
        return f"must be between {schema['minimum']} and {schema['maximum']}"
    return None
