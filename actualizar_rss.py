from __future__ import annotations

import email.utils
import html
import json
import os
import re
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup


BASE = "https://www.vozpopuli.com"

SITEMAP_GENERAL = (
    f"{BASE}/sitemaps/posts-and-categories.xml"
)

SITEMAP_NOTICIAS = (
    f"{BASE}/sitemaps/google-news.xml"
)

SITEMAPS_ADICIONALES = [
    f"{BASE}/indux/news-sitemap.xml",
    f"{BASE}/indux/sitemap_google_news.xml",
]

SALIDA = Path("rss.xml")
ESTADO = Path("estado.json")

MAXIMO_ARTICULOS_RSS = 1500
MAXIMO_URL_ESTADO = 30000
MAXIMO_NUEVOS_POR_EJECUCION = 200
TRABAJADORES = 8

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/140 Safari/537.36"
)

CABECERAS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml,"
        "text/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "es-ES,es;q=0.9",
    "Cache-Control": "no-cache",
}

NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
    "image": "http://www.google.com/schemas/sitemap-image/1.1",
}


def descargar(url: str, timeout: int = 60) -> bytes:
    peticion = urllib.request.Request(
        url,
        headers=CABECERAS,
    )

    with urllib.request.urlopen(
        peticion,
        timeout=timeout,
    ) as respuesta:
        contenido = respuesta.read()

    if not contenido:
        raise RuntimeError(f"Respuesta vacía: {url}")

    return contenido


def limpiar_url(url: str) -> str:
    return url.strip().split("#", 1)[0].split("?", 1)[0]


def es_articulo(url: str) -> bool:
    if not url.startswith(BASE):
        return False

    ruta = urlparse(url).path.lower()

    # Los artículos de Vozpópuli terminan en .html
    if not ruta.endswith(".html"):
        return False

    exclusiones = (
        "/tag/",
        "/autor/",
        "/buscar/",
        "/servicios/",
        "/corporativo/",
        "/cotizaciones/",
        "/portal-lector/",
        "/mi-perfil/",
        "/loteria/",
        "/sitemaps/",
        "/wp-admin/",
        "/tdb_templates/",
    )

    return not any(
        exclusion in ruta
        for exclusion in exclusiones
    )


def leer_sitemap_general() -> set[str]:
    urls: set[str] = set()

    try:
        raiz = ET.fromstring(descargar(SITEMAP_GENERAL))
    except Exception as error:
        print(f"No se pudo leer el sitemap general: {error}")
        return urls

    for nodo in raiz.findall("sm:url", NS):
        url = nodo.findtext(
            "sm:loc",
            default="",
            namespaces=NS,
        )

        url = limpiar_url(url)

        if es_articulo(url):
            urls.add(url)

    return urls


def leer_sitemap_noticias(url_sitemap: str) -> dict[str, dict]:
    noticias: dict[str, dict] = {}

    try:
        raiz = ET.fromstring(descargar(url_sitemap))
    except Exception as error:
        print(f"No se pudo leer {url_sitemap}: {error}")
        return noticias

    for nodo in raiz.findall("sm:url", NS):
        url = nodo.findtext(
            "sm:loc",
            default="",
            namespaces=NS,
        )

        url = limpiar_url(url)

        if not es_articulo(url):
            continue

        bloque_noticia = nodo.find("news:news", NS)

        titulo = ""
        fecha = ""

        if bloque_noticia is not None:
            titulo = bloque_noticia.findtext(
                "news:title",
                default="",
                namespaces=NS,
            ).strip()

            fecha = bloque_noticia.findtext(
                "news:publication_date",
                default="",
                namespaces=NS,
            ).strip()

        imagen = nodo.findtext(
            "image:image/image:loc",
            default="",
            namespaces=NS,
        ).strip()

        noticias[url] = {
            "url": url,
            "titulo": titulo,
            "fecha": fecha,
            "imagen": imagen,
        }

    return noticias


def leer_noticias_recientes() -> dict[str, dict]:
    noticias: dict[str, dict] = {}

    sitemaps = [
        SITEMAP_NOTICIAS,
        *SITEMAPS_ADICIONALES,
    ]

    with ThreadPoolExecutor(
        max_workers=len(sitemaps)
    ) as ejecutor:
        trabajos = {
            ejecutor.submit(
                leer_sitemap_noticias,
                sitemap,
            ): sitemap
            for sitemap in sitemaps
        }

        for trabajo in as_completed(trabajos):
            noticias.update(trabajo.result())

    return noticias


def cargar_estado() -> tuple[set[str], bool]:
    if not ESTADO.exists():
        return set(), True

    try:
        datos = json.loads(
            ESTADO.read_text(encoding="utf-8")
        )

        return set(datos.get("urls_vistas", [])), False

    except (json.JSONDecodeError, OSError):
        return set(), True


def guardar_texto_atomico(
    ruta: Path,
    contenido: str,
) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=".",
        prefix=f"{ruta.stem}_",
        suffix=".tmp",
    ) as temporal:
        temporal.write(contenido)
        temporal_path = Path(temporal.name)

    os.replace(temporal_path, ruta)


def guardar_estado(urls: set[str]) -> None:
    lista = sorted(urls)[-MAXIMO_URL_ESTADO:]

    datos = {
        "ultima_actualizacion": datetime.now(
            timezone.utc
        ).isoformat(),
        "urls_vistas": lista,
    }

    guardar_texto_atomico(
        ESTADO,
        json.dumps(
            datos,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
    )


def obtener_meta(
    sopa: BeautifulSoup,
    nombre: str,
    atributo: str = "property",
) -> str:
    etiqueta = sopa.find(
        "meta",
        attrs={atributo: nombre},
    )

    if not etiqueta:
        return ""

    return etiqueta.get("content", "").strip()


def convertir_fecha(fecha: str) -> datetime:
    if not fecha:
        return datetime.now(timezone.utc)

    fecha = fecha.strip().replace("Z", "+00:00")

    try:
        resultado = datetime.fromisoformat(fecha)

        if resultado.tzinfo is None:
            resultado = resultado.replace(
                tzinfo=timezone.utc
            )

        return resultado

    except ValueError:
        try:
            resultado = email.utils.parsedate_to_datetime(fecha)

            if resultado.tzinfo is None:
                resultado = resultado.replace(
                    tzinfo=timezone.utc
                )

            return resultado

        except (TypeError, ValueError):
            return datetime.now(timezone.utc)


def categoria_desde_url(url: str) -> str:
    ruta = urlparse(url).path.lower()

    categorias = [
        ("/economia/empresas/", "Empresas"),
        ("/economia/banca/", "Banca"),
        ("/economia/energia/", "Energía"),
        ("/economia/transporte/", "Transporte"),
        ("/economia/inmobiliario/", "Inmobiliario"),
        ("/economia/distribucion/", "Distribución y consumo"),
        ("/economia/fiscal/", "Fiscal"),
        ("/economia/macroeconomia/", "Macroeconomía"),
        ("/economia/", "Economía"),
        ("/espana/politica/", "Política"),
        ("/espana/defensa/", "Defensa"),
        ("/espana/tribunales/", "Tribunales"),
        ("/espana/", "España"),
        ("/internacional/", "Internacional"),
        ("/opinion/", "Opinión"),
        ("/actualidad/", "Actualidad"),
        ("/altavoz/cultura/", "Cultura"),
        ("/altavoz/historia/", "Historia"),
        ("/altavoz/libros/", "Libros"),
        ("/altavoz/musica/", "Música"),
        ("/altavoz/tecnologia/", "Tecnología"),
        ("/altavoz/ciencia/", "Ciencia"),
        ("/altavoz/", "Zona Abierta"),
        ("/medios/", "Medios y televisión"),
        ("/deportes/", "Deportes"),
        ("/bienestar/", "Bienestar"),
        ("/gastronomia/", "Gastronomía"),
        ("/motor/", "Motor"),
        ("/sanitatem/", "Sanitatem"),
        ("/indux/", "Indux"),
        ("/dolcevita/", "DolceVita"),
        ("/la-voz-de-las-marcas/", "La Voz de las Marcas"),
    ]

    for patron, categoria in categorias:
        if patron in ruta:
            return categoria

    primera_carpeta = ruta.strip("/").split("/", 1)[0]

    if primera_carpeta:
        return primera_carpeta.replace("-", " ").title()

    return "Vozpópuli"


def extraer_articulo(
    url: str,
    datos_sitemap: dict | None = None,
) -> dict | None:
    datos_sitemap = datos_sitemap or {}

    try:
        contenido = descargar(url, timeout=45)
        sopa = BeautifulSoup(contenido, "html.parser")

        titulo = (
            obtener_meta(sopa, "og:title")
            or datos_sitemap.get("titulo", "")
        )

        if not titulo and sopa.title:
            titulo = sopa.title.get_text(
                " ",
                strip=True,
            )

        descripcion = (
            obtener_meta(sopa, "description", "name")
            or obtener_meta(sopa, "og:description")
        )

        fecha = (
            obtener_meta(sopa, "article:published_time")
            or obtener_meta(sopa, "datePublished")
            or datos_sitemap.get("fecha", "")
        )

        autor = (
            obtener_meta(sopa, "author", "name")
            or obtener_meta(sopa, "article:author")
        )

        imagen = (
            obtener_meta(sopa, "og:image")
            or datos_sitemap.get("imagen", "")
        )

        titulo = html.unescape(
            " ".join(titulo.split())
        )

        descripcion = html.unescape(
            " ".join(descripcion.split())
        )

        if not titulo:
            return None

        return {
            "url": url,
            "titulo": titulo,
            "descripcion": descripcion,
            "fecha": convertir_fecha(fecha),
            "categoria": categoria_desde_url(url),
            "autor": autor,
            "imagen": imagen,
        }

    except Exception as error:
        print(f"No se pudo procesar {url}: {error}")
        return None


def texto_elemento(
    elemento: ET.Element,
    nombre: str,
) -> str:
    nodo = elemento.find(nombre)

    if nodo is None or nodo.text is None:
        return ""

    return nodo.text.strip()


def cargar_rss_anterior() -> dict[str, ET.Element]:
    articulos: dict[str, ET.Element] = {}

    if not SALIDA.exists():
        return articulos

    try:
        raiz = ET.parse(SALIDA).getroot()
        canal = raiz.find("channel")

        if canal is None:
            return articulos

        for item in canal.findall("item"):
            url = (
                texto_elemento(item, "guid")
                or texto_elemento(item, "link")
            )

            url = limpiar_url(url)

            if url:
                articulos[url] = item

    except ET.ParseError:
        print("El rss.xml anterior no era válido")

    return articulos


def fecha_item(item: ET.Element) -> datetime:
    return convertir_fecha(
        texto_elemento(item, "pubDate")
    )


def crear_item(datos: dict) -> ET.Element:
    item = ET.Element("item")

    ET.SubElement(item, "title").text = datos["titulo"]
    ET.SubElement(item, "link").text = datos["url"]

    guid = ET.SubElement(
        item,
        "guid",
        {"isPermaLink": "true"},
    )
    guid.text = datos["url"]

    ET.SubElement(item, "pubDate").text = (
        email.utils.format_datetime(datos["fecha"])
    )

    ET.SubElement(item, "category").text = (
        datos["categoria"]
    )

    if datos.get("descripcion"):
        ET.SubElement(item, "description").text = (
            datos["descripcion"]
        )

    if datos.get("autor"):
        ET.SubElement(item, "author").text = (
            datos["autor"]
        )

    if datos.get("imagen"):
        ET.SubElement(
            item,
            "enclosure",
            {
                "url": datos["imagen"],
                "type": "image/jpeg",
            },
        )

    return item


def crear_rss(
    articulos: dict[str, ET.Element],
) -> ET.ElementTree:
    ordenados = sorted(
        articulos.values(),
        key=fecha_item,
        reverse=True,
    )[:MAXIMO_ARTICULOS_RSS]

    rss = ET.Element("rss", {"version": "2.0"})
    canal = ET.SubElement(rss, "channel")

    ET.SubElement(canal, "title").text = (
        "Vozpópuli — Todas las publicaciones"
    )

    ET.SubElement(canal, "link").text = BASE

    ET.SubElement(canal, "description").text = (
        "Todas las noticias públicas de Vozpópuli, "
        "incluidas Economía y Empresas."
    )

    ET.SubElement(canal, "language").text = "es-ES"

    ET.SubElement(canal, "lastBuildDate").text = (
        email.utils.format_datetime(
            datetime.now(timezone.utc)
        )
    )

    for item in ordenados:
        canal.append(item)

    return ET.ElementTree(rss)


def guardar_xml_atomico(arbol: ET.ElementTree) -> None:
    ET.indent(arbol, space="  ")

    with tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=".",
        prefix="rss_",
        suffix=".xml",
    ) as temporal:
        temporal_path = Path(temporal.name)

        arbol.write(
            temporal,
            encoding="utf-8",
            xml_declaration=True,
        )

    os.replace(temporal_path, SALIDA)


def main() -> None:
    noticias_recientes = leer_noticias_recientes()
    urls_generales = leer_sitemap_general()

    urls_vistas, primera_ejecucion = cargar_estado()
    articulos = cargar_rss_anterior()

    if primera_ejecucion:
        # Registra el sitemap completo, pero no llena Feedly
        # con miles de artículos antiguos.
        candidatos = set(noticias_recientes)

        print(
            "Primera ejecución: se añadirán "
            f"{len(candidatos)} noticias recientes"
        )
    else:
        nuevas = urls_generales - urls_vistas

        candidatos = set(noticias_recientes) | nuevas

        nuevas_empresas = [
            url
            for url in nuevas
            if "/economia/empresas/" in url.lower()
        ]

        print(f"Nuevos artículos detectados: {len(nuevas)}")
        print(
            "Nuevos artículos de empresas: "
            f"{len(nuevas_empresas)}"
        )

    pendientes = [
        url
        for url in candidatos
        if url not in articulos
    ]

    pendientes = pendientes[:MAXIMO_NUEVOS_POR_EJECUCION]

    resultados: list[dict] = []

    with ThreadPoolExecutor(
        max_workers=TRABAJADORES
    ) as ejecutor:
        trabajos = {
            ejecutor.submit(
                extraer_articulo,
                url,
                noticias_recientes.get(url),
            ): url
            for url in pendientes
        }

        for trabajo in as_completed(trabajos):
            resultado = trabajo.result()

            if resultado:
                resultados.append(resultado)

    for resultado in resultados:
        articulos[resultado["url"]] = crear_item(resultado)

    guardar_xml_atomico(crear_rss(articulos))

    urls_vistas.update(urls_generales)
    urls_vistas.update(noticias_recientes)
    guardar_estado(urls_vistas)

    empresas_anadidas = sum(
        1
        for resultado in resultados
        if resultado["categoria"] == "Empresas"
    )

    print(
        f"RSS actualizado: {len(articulos)} publicaciones"
    )
    print(
        f"Nuevas añadidas: {len(resultados)}"
    )
    print(
        f"De empresas añadidas: {empresas_anadidas}"
    )


if __name__ == "__main__":
    main()
