"""Offline price recommendations based exclusively on validated agent outputs."""
import argparse
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from pricing_recommendation import calculate

from .exchange import Product, clock, load, number, timestamp, validate

MAX_AGE = timedelta(hours=24)


def eligibility(row, now):
    age = now - timestamp(row["checked_at"])
    if age < timedelta(0):
        return "Дата проверки в будущем"
    if age > MAX_AGE:
        return "Старше 24 часов"
    if row["status"] != "confirmed":
        return "Требуется подтверждение"
    if row["availability"] != "in_stock":
        return "Наличие не подтверждено"
    return ""


def quote_rows(competitors, supplier, costs, now):
    groups = defaultdict(lambda: ([], []))
    for index, rows in enumerate((competitors, supplier)):
        for row in rows:
            groups[Product(**row["product"]).key()][index].append(row)
    for retail, wholesale in groups.values():
        product = Product(**(wholesale or retail)[0]["product"])
        market = current_quotes(retail, now)
        purchase = min(current_quotes(wholesale, now), key=lambda row: number(row["price_rub"]), default=None)
        if purchase is None:
            yield product, {"status": "Нет подтверждённой закупки в наличии"}
            continue
        buy = number(purchase["price_rub"])
        competitor = min(market, key=lambda row: number(row["price_rub"]), default=None)
        market_price = number(competitor["price_rub"]) if competitor else None
        values = calculate(buy, competitor_min=market_price, minimum_markup=0, **costs)
        yield product, {
            "status": "Рекомендация" if competitor else "Минимум без рыночного ориентира",
            "purchase": float(buy), "supplier_url": purchase["url"], "supplier_variant": purchase["variant"],
            "market": float(market_price) if competitor else None,
            "market_url": competitor["url"] if competitor else None,
            "minimum": values["minimum"], "recommended": values["recommended"],
            "total_cost": round(float(buy) + values["costs"], 2), "profit": values["profit"],
        }


def current_quotes(rows, now):
    latest = defaultdict(list)
    for row in rows:
        if row["status"] == "confirmed" and timestamp(row["checked_at"]) <= now:
            latest[row["source"], row["url"], row["variant"]].append(row)
    for group in latest.values():
        newest = max(timestamp(row["checked_at"]) for row in group)
        candidates = [row for row in group if timestamp(row["checked_at"]) == newest]
        if len({(number(row["price_rub"]), row["availability"]) for row in candidates}) == 1:
            row = candidates[0]
            if not eligibility(row, now):
                yield row


def add_table(sheet, headers, rows):
    sheet.append(headers)
    for values in rows:
        sheet.append(values)
        for cell in sheet[sheet.max_row]:
            if cell.data_type == "f" or cell.data_type == "e":
                cell.data_type = "s"
    for column in sheet.columns:
        column[0].font = Font(bold=True)
        width = max(len(str(cell.value or "")) for cell in column) + 2
        sheet.column_dimensions[get_column_letter(column[0].column)].width = min(width, 65)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions


def identity_cells(product):
    return [product.brand, product.name, product.concentration, float(number(product.volume_ml)),
            product.package, product.configuration]


def write_report(path, competitors, supplier, costs, now):
    book = Workbook()
    sheet = book.active
    sheet.title = "Рекомендации"
    identity = ["Бренд", "Название", "Концентрация", "Объём, мл", "Упаковка", "Комплектация"]
    fields = {
        "status": "Решение", "purchase": "Закупка, ₽", "supplier_url": "Карточка поставщика",
        "supplier_variant": "Предложение", "market": "Цена конкурента, ₽", "market_url": "Карточка конкурента",
        "minimum": "Минимальная цена, ₽", "recommended": "Рекомендованная цена, ₽",
        "total_cost": "Себестоимость с расходами, ₽", "profit": "Остаток, ₽",
    }
    add_table(sheet, identity + list(fields.values()),
              (identity_cells(product) + [quote.get(key) for key in fields]
               for product, quote in quote_rows(competitors, supplier, costs, now)))
    keys = ("source", "status", "availability", "checked_at", "url", "variant", "evidence", "reason")
    for title, rows in (("Конкуренты", competitors), ("Поставщик", supplier)):
        add_table(book.create_sheet(title), identity + [
            "Источник", "Статус", "Наличие", "Проверено", "URL", "Вариант", "Подтверждение",
            "Примечание", "Цена, ₽", "Проверка актуальности и наличия",
        ], (identity_cells(Product(**row["product"])) + [row[key] for key in keys]
            + [float(number(row["price_rub"])) if row["price_rub"] is not None else None,
               eligibility(row, now)] for row in rows))
    cost_labels = {
        "shipping": "Доставка и упаковка на единицу, ₽", "other": "Прочие расходы на единицу, ₽",
        "commission_pct": "Комиссия с продажи, %", "tax_pct": "Налог с продажи, %",
        "markup_pct": "Минимальная наценка к закупке после расходов, %", "undercut": "Снижение от цены конкурента, ₽",
    }
    add_table(book.create_sheet("Параметры"), ["Параметр", "Значение"],
              [("Дата расчёта (UTC)", now.isoformat()), ("Окно актуальности, ч", 24)]
              + [(cost_labels[key], float(value)) for key, value in costs.items()])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, suffix=".xlsx", delete=False) as file:
        temp = Path(file.name)
    try:
        book.save(temp)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--competitors", required=True)
    parser.add_argument("--supplier", required=True)
    parser.add_argument("--output", required=True)
    for flag in ("shipping", "other", "commission-pct", "tax-pct"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--markup-pct", default="15")
    parser.add_argument("--undercut", default="100")
    args = parser.parse_args()
    if Path(args.output).suffix.lower() != ".xlsx":
        parser.error("--output должен иметь расширение .xlsx")
    try:
        costs = {key: number(getattr(args, key), zero=True) for key in (
            "shipping", "other", "commission_pct", "tax_pct", "markup_pct", "undercut")}
        if costs["commission_pct"] + costs["tax_pct"] >= 100:
            raise ValueError("Комиссия + налог должны быть меньше 100%")
        competitors = validate(load(args.competitors), "competitors")
        supplier = validate(load(args.supplier), "supplier")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        parser.error(str(exc))
    write_report(args.output, competitors, supplier, costs, clock())


if __name__ == "__main__":
    main()
