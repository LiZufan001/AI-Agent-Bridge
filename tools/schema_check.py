"""Bounded validator for the JSON Schema vocabulary used by this Engine.

No network schema resolution, no alternate protocol transition implementation.
Unknown vocabulary fails closed so a future schema cannot silently lose checks.
"""
from __future__ import annotations
import json
import re
from datetime import datetime
from typing import Any

SUPPORTED = {'$schema','$id','title','description','type','const','enum','properties',
    'additionalProperties','required','allOf','oneOf','not','if','then','else','items',
    'minItems','maxItems','uniqueItems','minimum','maximum','minLength','maxLength','pattern','format'}


def strict_json(raw: bytes | str, *, limit: int = 8 * 1024 * 1024) -> Any:
    if len(raw) > limit:
        raise ValueError('JSON exceeds size limit')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('nonfinite JSON value')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def _equal(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def validate(value: Any, schema: dict | bool, path: str = '$', depth: int = 0) -> list[str]:
    if depth > 64:
        return [path + ': schema nesting limit']
    if schema is True:
        return []
    if schema is False:
        return [path + ': forbidden by schema']
    unknown = set(schema) - SUPPORTED
    if unknown:
        return [path + ': unsupported schema vocabulary ' + ','.join(sorted(unknown))]
    errors = []
    types = schema.get('type', [])
    if isinstance(types, str):
        types = [types]
    actual = ('null' if value is None else 'boolean' if type(value) is bool else
              'integer' if type(value) is int else 'number' if type(value) is float else
              'string' if isinstance(value, str) else 'object' if isinstance(value, dict) else
              'array' if isinstance(value, list) else 'unknown')
    if types and actual not in types and not (actual == 'integer' and 'number' in types):
        return [path + ': type mismatch']
    if 'const' in schema and not _equal(value, schema['const']):
        errors.append(path + ': const mismatch')
    if 'enum' in schema and not any(_equal(value, option) for option in schema['enum']):
        errors.append(path + ': enum mismatch')
    if isinstance(value, dict):
        for key in schema.get('required', []):
            if key not in value:
                errors.append(path + ': missing ' + key)
        properties = schema.get('properties', {})
        for key, item in value.items():
            if key in properties:
                errors += validate(item, properties[key], path + '.' + key, depth + 1)
            elif 'additionalProperties' in schema:
                errors += validate(item, schema['additionalProperties'], path + '.' + key, depth + 1)
    if isinstance(value, list):
        if len(value) < schema.get('minItems', 0) or len(value) > schema.get('maxItems', 1000000):
            errors.append(path + ': array length')
        if schema.get('uniqueItems') and len({json.dumps(x, sort_keys=True) for x in value}) != len(value):
            errors.append(path + ': duplicate array items')
        for i, item in enumerate(value):
            if 'items' in schema:
                errors += validate(item, schema['items'], path + f'[{i}]', depth + 1)
    if type(value) in {int, float}:
        if value < schema.get('minimum', float('-inf')) or value > schema.get('maximum', float('inf')):
            errors.append(path + ': numeric bound')
    if isinstance(value, str):
        if len(value) < schema.get('minLength', 0) or len(value) > schema.get('maxLength', 1000000000):
            errors.append(path + ': string length')
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            errors.append(path + ': pattern mismatch')
        if 'format' in schema:
            if schema['format'] == 'date-time':
                try:
                    if datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is None:
                        raise ValueError('timezone missing')
                except ValueError:
                    errors.append(path + ': invalid zoned date-time')
            else:
                errors.append(path + ': unsupported schema format')
    for option in schema.get('allOf', []):
        errors += validate(value, option, path, depth + 1)
    if 'oneOf' in schema and sum(not validate(value, option, path, depth + 1) for option in schema['oneOf']) != 1:
        errors.append(path + ': oneOf requires exactly one match')
    if 'not' in schema and not validate(value, schema['not'], path, depth + 1):
        errors.append(path + ': forbidden shape')
    if 'if' in schema:
        branch = 'else' if validate(value, schema['if'], path, depth + 1) else 'then'
        if branch in schema:
            errors += validate(value, schema[branch], path, depth + 1)
    return errors
