"""Kargo bedeli açıkça verilirse (mağaza fiyatı kargo hariç, kargo_tl=0) kargo baremi ön koşul olmamalı."""
import mcp_server

BAREM = "Kargo baremi (desi aralığı ve bedeli)"


def test_kargo_baremi_yalnizca_desi_ile_hesapta_gerekli(session):
    assert BAREM in mcp_server._eksikler(session)
    assert BAREM not in mcp_server._eksikler(session, kargo_gerekli=False)
