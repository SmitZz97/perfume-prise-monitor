"""Create a public, synthetic input list; no prices, credentials or supplier URLs."""
from pathlib import Path
from openpyxl import Workbook

PRODUCTS = [
    ("Creed Aventus EDP", 100),
    ("Ex Nihilo Fleur Narcotique EDP", 100),
    ("Creed Green Irish Tweed EDP", 100),
    ("Armaf Club De Nuit Intense Man EDT", 105),
    ("Attar Collection Musk Kashmir EDP", 100),
]


def create_test_input(path=None):
    path = Path(path) if path else Path(__file__).resolve().parent / "test_products.xlsx"
    if path.exists():
        raise FileExistsError(f"Файл уже существует и не изменён: {path}")
    wb = Workbook()
    ws = wb.active
    ws.title = "Ароматы"
    ws.append(["Аромат", "Объём, мл"])
    for name, volume in PRODUCTS:
        ws.append([name, volume])
    wb.save(path)
    return path


if __name__ == "__main__":
    print(create_test_input())
