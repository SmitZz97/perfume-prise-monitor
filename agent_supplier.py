"""Agent 2: independently read matching offers at application.delice-opt.ru."""

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from agent_protocol import checkpoint, load_specs, new_snapshot
from monitor_core import result
from supplier_compare import assess_offer, norm, read_live

ROOT = Path(__file__).resolve().parent


def supplier_link(name, volume, links):
    from monitor_core import scent_name
    for lookup in (norm(name) + '|' + str(volume), norm(scent_name(name)) + '|' + str(volume)):
        if lookup in links:
            return str(links[lookup]).strip()
    return ''


def allowed_supplier_url(url):
    parsed = urlsplit(str(url))
    return (parsed.scheme == 'https' and parsed.netloc == 'application.delice-opt.ru' and
            parsed.username is None and parsed.password is None and not parsed.fragment and
            bool(re.fullmatch(r'/catalog/\d+/?', parsed.path)))


def collect_supplier(name, volume, *, url='', disabled=False, fetch_offers=None, profile=None):
    row = result('delice opt', name, volume, url=url)
    row.update(variants=[], source='', availability='unknown')
    if disabled:
        row['reason'] = 'Агент поставщика отключён; текущая закупка неизвестна'
        return row
    if not url:
        row['reason'] = 'Нет ссылки на товар Delice Opt; добавьте её в supplier_links.json'
        return row
    if not allowed_supplier_url(url):
        row['reason'] = 'Некорректная ссылка: требуется https://application.delice-opt.ru/catalog/ЧИСЛО'
        return row
    if fetch_offers is None and not (Path(profile) if profile else ROOT / 'supplier_browser_profile').is_dir():
        row['reason'] = 'Профиль Delice Opt удалён или отсутствует; требуется повторный вход на Windows'
        return row
    try:
        offers = (fetch_offers or read_live)(url)
    except Exception as exc:
        row['reason'] = 'Не удалось прочитать текущую карточку Delice Opt: ' + type(exc).__name__
        return row
    valid = []
    for offer in offers:
        check = assess_offer(offer, name, volume)
        row['variants'].append({
            'title': offer.get('title', ''), 'brand': offer.get('brand', ''),
            'volume': offer.get('volume', ''), 'supplier': offer.get('supplier', ''),
            'price_rub': check['price'], 'confirmed': bool(check['confirmed']),
            'identity_verified': bool(check['identity_verified']),
            'concentration_verified': bool(check['concentration_verified']),
            'volume_verified': bool(check['volume_verified']),
            'reason': check['reason'], 'url': url,
        })
        if check['confirmed']:
            valid.append((check['price'], offer))
    if not valid:
        row['reason'] = ('; '.join(dict.fromkeys(v['reason'] for v in row['variants'] if v['reason']))[:500]
                         or 'В текущей карточке не найдено точного варианта с однозначной ценой')
        return row
    price, offer = min(valid, key=lambda item: item[0])
    row.update(price_rub=price, confirmed=True, found=True, identity_verified=True,
               volume_verified=True, concentration_verified=True, price_received=True,
               status='ok', source='automatically', seller=offer.get('supplier', ''),
               price_source='Текущая строка Delice Opt',
               reason='Цена подтверждена; наличие и итоговую стоимость заказа нужно перепроверить')
    return row


def run_supplier(specs, output, links, *, disabled=False, fetch_offers=None, profile=None):
    snapshot = new_snapshot('supplier')
    for spec in specs:
        name, volume = spec['query'], spec['volume_ml']
        url = supplier_link(name, volume, links)
        record = collect_supplier(name, volume, url=url, disabled=disabled,
                                  fetch_offers=fetch_offers, profile=profile)
        snapshot['products'].append({'spec': spec, 'records': [record]})
        checkpoint(snapshot, output)
        print(f'Поставщик: {name}, {volume} мл — {"закупка подтверждена" if record["confirmed"] else record["reason"]}', flush=True)
    return snapshot


def main():
    ap = argparse.ArgumentParser(description='Агент №2: только Delice Opt')
    ap.add_argument('--input', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('--links-file', default=str(ROOT / 'supplier_links.json'), help='Локальные ссылки на карточки поставщика')
    ap.add_argument('--supplier-disabled', action='store_true', help='Не обращаться к сайту поставщика')
    args = ap.parse_args()
    try:
        specs = load_specs(args.input)
        source = Path(args.links_file)
        links = json.loads(source.read_text(encoding='utf-8')) if source.exists() else {}
        if not isinstance(links, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in links.items()):
            raise ValueError('Реестр supplier_links.json должен быть объектом {"аромат|объём": "URL"}')
    except (ValueError, OSError) as exc:
        ap.error(str(exc))
    run_supplier(specs, args.output, links, disabled=args.supplier_disabled)


if __name__ == '__main__':
    main()
