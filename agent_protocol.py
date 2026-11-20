"""Versioned, local-only contract between the three independent workers."""

import json
import math
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from monitor_core import STORES, concentration, money, plain_product, scent_name

VERSION = 1
TIMEZONE = ZoneInfo('Europe/Moscow')


def timestamp():
    return datetime.now(TIMEZONE).isoformat(timespec='seconds')


def spec_for(name, volume):
    name = str(name).strip()
    try:
        numeric = float(volume)
    except (ValueError, TypeError):
        raise ValueError(f'{name!r}: объём должен быть положительным целым числом') from None
    if not math.isfinite(numeric) or numeric <= 0 or not numeric.is_integer():
        raise ValueError(f'{name!r}: объём должен быть положительным целым числом')
    code = concentration(name)
    if not name or not scent_name(name) or not code or not plain_product(name):
        raise ValueError(f'{name!r}: укажите точный аромат и одну концентрацию EDP/EDT/Extrait/Parfum; без тестеров и наборов')
    volume = int(numeric)
    return {'id': json.dumps([scent_name(name), code, volume], ensure_ascii=False, separators=(',', ':')),
            'query': name, 'concentration': code, 'volume_ml': volume}


def load_specs(input_path):
    from batch_monitor import read_entries
    specs = [spec_for(name, volume) for name, volume in read_entries(input_path)]
    if not specs:
        raise ValueError('Список ароматов пуст')
    ids = [spec['id'] for spec in specs]
    if len(ids) != len(set(ids)):
        raise ValueError('В Excel один и тот же аромат, концентрация и объём повторяются')
    return specs


def new_snapshot(agent):
    if agent not in ('competitors', 'supplier'):
        raise ValueError('Неизвестный агент')
    return {'schema_version': VERSION, 'agent': agent, 'generated_at': timestamp(), 'products': []}


def checkpoint(snapshot, destination):
    """Never leave a half-written JSON snapshot after interruption."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.partial')
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(temporary, destination)


def read_snapshot(path, expected_agent):
    source = Path(path)
    data = json.loads(source.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('schema_version') != VERSION or data.get('agent') != expected_agent:
        raise ValueError(f'{source}: несовместимый или чужой отчёт агента')
    if not isinstance(data.get('products'), list):
        raise ValueError(f'{source}: отсутствует список товаров')
    ids = set()
    for item in data['products']:
        if not isinstance(item, dict) or not isinstance(item.get('spec'), dict) or not isinstance(item.get('records'), list):
            raise ValueError(f'{source}: неверная структура товара')
        spec = item['spec']
        if spec_for(spec.get('query'), spec.get('volume_ml')) != spec:
            raise ValueError(f'{source}: идентичность товара изменилась')
        if spec['id'] in ids:
            raise ValueError(f'{source}: повтор одного товара')
        ids.add(spec['id'])
        expected = set(STORES) if expected_agent == 'competitors' else {'delice opt'}
        records = item['records']
        if len(records) != len(expected) or {r.get('store') for r in records if isinstance(r, dict)} != expected:
            raise ValueError(f'{source}: неверный набор магазинов/поставщиков')
        for record in records:
            if record.get('query') != spec['query'] or record.get('volume_ml') != spec['volume_ml']:
                raise ValueError(f'{source}: запись цены относится к другому аромату или объёму')
    return data


def matching_products(competitors, supplier):
    left = {item['spec']['id']: item for item in competitors['products']}
    right = {item['spec']['id']: item for item in supplier['products']}
    if not left or left.keys() != right.keys():
        raise ValueError('Наборы товаров двух агентов не совпадают или пусты')
    for identity in left:
        if left[identity]['spec'] != right[identity]['spec']:
            raise ValueError('Запрос, объём или концентрация двух агентов различаются')
    return left, right


def fresh(record, *, at=None, ttl_hours=24):
    """Trust the observation time, never the report file's modification time."""
    try:
        checked = datetime.fromisoformat(record['checked_at'])
        at = at or datetime.now(TIMEZONE)
        if checked.utcoffset() is None or at.utcoffset() is None:
            return False
        age = at - checked
        if not timedelta(0) <= age < timedelta(hours=ttl_hours):
            return False
        if record.get('expires_at'):
            expiry = datetime.fromisoformat(record['expires_at'])
            if expiry.utcoffset() is None or at >= expiry:
                return False
        return True
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def usable(record, *, at=None, competitor=False):
    if not all(record.get(flag) is True for flag in ('confirmed', 'identity_verified', 'volume_verified', 'concentration_verified')):
        return False
    if money(record.get('price_rub')) is None or not fresh(record, at=at):
        return False
    if record.get('source') not in ('automatically', 'manually'):
        return False
    if competitor:
        if record.get('discount_kind') in ('loyalty', 'coupon') or re.search(
                r'по карте|с картой|по промокоду|для участников|\bloyalty\b|\bcoupon\b|\bmember\b',
                str(record.get('discount_conditions', '')), flags=re.I):
            return False
        return record.get('availability') == 'InStock' and record.get('third_party') is False
    return True
