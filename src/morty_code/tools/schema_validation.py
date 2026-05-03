from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ValidationErrorDetail:
    path: str
    message: str


class ToolInputValidationError(ValueError):
    """模型生成的 tool input 不符合工具 schema。"""

    def __init__(self, tool_name: str, errors: list[ValidationErrorDetail]) -> None:
        self.tool_name = tool_name
        self.errors = errors
        details = "; ".join(f"{error.path}: {error.message}" for error in errors)
        super().__init__(f"InputValidationError for {tool_name}: {details}")


def validate_tool_input(tool_name: str, schema: dict[str, Any] | None, value: dict[str, object]) -> None:
    if not schema:
        return
    errors: list[ValidationErrorDetail] = []
    _validate_schema(schema, value, "$", errors)
    if errors:
        raise ToolInputValidationError(tool_name, errors)


def _validate_schema(
    schema: dict[str, Any],
    value: object,
    path: str,
    errors: list[ValidationErrorDetail],
) -> None:
    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        if not any(_matches_type(item, value) for item in expected_type):
            errors.append(ValidationErrorDetail(path, f"expected one of {expected_type}"))
            return
    elif isinstance(expected_type, str) and not _matches_type(expected_type, value):
        errors.append(ValidationErrorDetail(path, f"expected {expected_type}"))
        return

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        errors.append(ValidationErrorDetail(path, f"expected one of {enum}"))
        return

    if expected_type == "object" or "properties" in schema:
        if not isinstance(value, dict):
            errors.append(ValidationErrorDetail(path, "expected object"))
            return
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in value:
                    errors.append(ValidationErrorDetail(f"{path}.{key}", "is required"))
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, child_schema in properties.items():
                if key not in value or not isinstance(child_schema, dict):
                    continue
                _validate_schema(child_schema, value[key], f"{path}.{key}", errors)
    if expected_type == "array":
        if not isinstance(value, list):
            errors.append(ValidationErrorDetail(path, "expected array"))
            return
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema(item_schema, item, f"{path}[{index}]", errors)


def _matches_type(expected_type: str, value: object) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return (isinstance(value, int | float) and not isinstance(value, bool))
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True
