# EVE Local Tool

Herramienta web local para evaluar la peligrosidad de una lista de personajes de **EVE Online** (por ejemplo, pegada desde el chat local del juego).

> **No oficial.** No afiliada a ni respaldada por CCP hf. Usa únicamente la API pública [ESI](https://developers.eveonline.com/) y las estadísticas públicas de [zKillboard](https://zkillboard.com/). La lista de personajes se pega manualmente; esta herramienta no lee memoria ni pantalla del cliente del juego.

## Qué hace

Pegás una lista de nombres de personajes y por cada uno resuelve:

- **Corp / alianza actual** (ESI)
- **Peligrosidad**: un puntaje "analizado" que parte del `dangerRatio` histórico de zKillboard y lo ajusta según señales de la lista pegada (compañeros de corp/alianza presentes, kills recientes coordinados con otros de la lista, tamaño del grupo detectado) y del propio historial del piloto (si su historial es mayormente *padding* — estructuras/naves sin piloto en flotas grandes —, si tiene actividad reciente genuina, o si lleva mucho tiempo inactivo)
- **Naves recientes** (últimos 3 días, si no hay nada se extiende a 7) y **nave favorita** histórica
- **Alertas**: posible alt de cyno, Black Ops/bombarderos, naves de guerra electrónica — con la nave específica detectada
- **Posible multiboxer**: nombres de la lista con patrones de nombre similares

## Cómo correrlo

```bash
pip install -r requirements.txt
python app.py
```

Abrí `http://localhost:5000` en el navegador.

## Estructura

- `app.py` — servidor Flask, arma el reporte y calcula el puntaje local por lote
- `eve_api.py` — clientes de ESI y zKillboard, detección de tipos de nave (estructuras, cyno, EWAR, Black Ops)
- `cache.py` — caché local en SQLite (6h para datos de personaje, indefinida para nombres/categorías de naves)
- `templates/` / `static/` — interfaz web

## Licencia y uso

Gratuito, sin fines de lucro, sin redistribuir los datos crudos de ESI/zKillboard como producto propio — en línea con el [Developer License Agreement](https://developers.eveonline.com/license-agreement) de CCP.
