"""Small JSON Schema subset validator for structured model output."""
from __future__ import annotations

def _matches(value, name):
    if name == 'integer':
        return isinstance(value, int) and not isinstance(value, bool)
    if name == 'number':
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    kinds = {'object': dict, 'array': list, 'string': str, 'boolean': bool, 'null': type(None)}
    return isinstance(value, kinds[name]) if name in kinds else True


def check(value, schema, path='$'):
    """Raise ValueError naming the first violated constraint."""
    if not isinstance(schema, dict):
        return
    kind = schema.get('type')
    if kind is not None and not any(_matches(value, name) for name in (kind if isinstance(kind, list) else [kind])):
        raise ValueError(f'{path}: expected {kind}')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'{path}: not an allowed value')
    if isinstance(value, dict):
        for name in schema.get('required', []):
            if name not in value:
                raise ValueError(f'{path}: missing {name}')
        properties = schema.get('properties', {})
        if schema.get('additionalProperties') is False:
            extra = sorted(set(value)-set(properties))
            if extra:
                raise ValueError(f'{path}: unexpected {extra[0]}')
        for name, child in properties.items():
            if name in value:
                check(value[name], child, f'{path}.{name}')
    elif isinstance(value, list):
        if len(value) < schema.get('minItems', 0):
            raise ValueError(f'{path}: too few items')
        if len(value) > schema.get('maxItems', len(value)):
            raise ValueError(f'{path}: too many items')
        for index, item in enumerate(value):
            check(item, schema.get('items'), f'{path}[{index}]')
    elif isinstance(value, str):
        if len(value) < schema.get('minLength', 0) or len(value) > schema.get('maxLength', len(value)):
            raise ValueError(f'{path}: bad length')
    elif _matches(value, 'number'):
        if value < schema.get('minimum', value) or value > schema.get('maximum', value):
            raise ValueError(f'{path}: out of range')


def coerce(value, schema):
    """Trim over-long strings and over-full arrays to the schema's limits; everything else is left for check().

    Providers that do not enforce length limits (or were sent a relaxed schema) often overrun a cap by a few
    characters in free-text notes. Trimming keeps the answer; type, enum and required errors still fail.
    """
    if not isinstance(schema, dict):
        return value
    if isinstance(value, dict) and isinstance(schema.get('properties'), dict):
        return {key: coerce(item, schema['properties'][key]) if key in schema['properties'] else item
                for key, item in value.items()}
    if isinstance(value, list):
        limit = schema.get('maxItems')
        items = value[:limit] if isinstance(limit, int) and len(value) > limit else value
        return [coerce(item, schema.get('items')) for item in items]
    if isinstance(value, str):
        limit = schema.get('maxLength')
        if isinstance(limit, int) and len(value) > limit and 'enum' not in schema:
            cut = value[:limit - 1].rstrip()
            space = cut.rfind(' ')
            return (cut[:space] if space > limit // 2 else cut).rstrip(' ,;:') + '…'
    return value
