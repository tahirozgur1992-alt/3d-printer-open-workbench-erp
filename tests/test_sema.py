"""Eski şemalı veritabanı açılır, eksik sütunlar eklenir ve eski kayıtlar durur."""
import sqlite3

from sqlalchemy import create_engine, inspect

import database


ESKI_SEMA = """
CREATE TABLE globalsettings (id INTEGER PRIMARY KEY, brand_name VARCHAR);
CREATE TABLE machine (id INTEGER PRIMARY KEY, name VARCHAR, brand_model VARCHAR);
CREATE TABLE product (id INTEGER PRIMARY KEY, sku VARCHAR, name VARCHAR);
CREATE TABLE productionorder (id INTEGER PRIMARY KEY, order_code VARCHAR);
CREATE TABLE productionqueueitem (id INTEGER PRIMARY KEY, product_id INTEGER, machine_id INTEGER, status VARCHAR);
INSERT INTO machine VALUES (1, 'Eski makine', 'Centauri');
INSERT INTO product VALUES (1, 'PRT-ESKI', 'Eski ürün');
INSERT INTO productionqueueitem VALUES (1, 1, 1, 'BEKLIYOR');
"""


def test_eski_veritabanina_sutunlar_eklenir_kayitlar_durur(tmp_path):
    yol = tmp_path / "eski.db"
    with sqlite3.connect(yol) as c:
        c.executescript(ESKI_SEMA)
    motor = create_engine(f"sqlite:///{yol}")
    with motor.begin() as b:
        eklenen = database.sema_guncelle(b)
    assert len(eklenen) == len(database.EK_SUTUNLAR)
    with motor.begin() as b:
        for tablo, sutun, _ in database.EK_SUTUNLAR:
            assert sutun in {s["name"] for s in inspect(b).get_columns(tablo)}
        assert database.sema_guncelle(b) == []  # ikinci çalıştırma bir şey değiştirmez
    with sqlite3.connect(yol) as c:
        assert c.execute("SELECT sku, urun_tipi, gozetimsiz_baski, gcode_path FROM product").fetchall() == [
            ("PRT-ESKI", "STANDART", 0, None)
        ]
        assert c.execute("SELECT name, yuklu_filament_id FROM machine").fetchall() == [("Eski makine", None)]
        assert c.execute("SELECT status, ozel_metin FROM productionqueueitem").fetchall() == [("BEKLIYOR", None)]


def test_yeni_veritabaninda_alanlar_var(session):
    from sqlalchemy import inspect as denetle

    d = denetle(database.engine)
    for tablo, sutun, _ in database.EK_SUTUNLAR:
        assert sutun in {s["name"] for s in d.get_columns(tablo)}, f"{tablo}.{sutun}"
