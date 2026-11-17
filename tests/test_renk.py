from fastapi.testclient import TestClient

import main


def _istemci(session):
    main.app.dependency_overrides[main.get_session] = lambda: session
    return TestClient(main.app)


def test_filament_renk_duzelt(session):
    c = _istemci(session)
    f = c.post("/api/filaments", json={"code": "T-RENK", "brand": "Sunlu", "material_type": "Mat PLA",
                                        "color_name": "Kemik", "price_per_kg_tl": 650}).json()
    r = c.patch(f"/api/filaments/{f['id']}/renk", json={"color_hex": "#e2ddc7"})
    assert r.status_code == 200 and r.json()["filament"]["color_hex"] == "#E2DDC7" and r.json()["eski_hex"] == "#1F2937"
    assert c.patch(f"/api/filaments/{f['id']}/renk", json={"color_hex": "beyaz"}).status_code == 400
    main.app.dependency_overrides.clear()
