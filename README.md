# Itinerario Parser API

API serverless en Vercel para parsear itinerarios de Aeromexico desde archivos PDF.

## URL de Producción

```
https://intinerario-api.vercel.app/api/parse
```

## Endpoints

### GET /api/parse

Verifica el estado del servicio.

**Respuesta:**
```json
{
  "status": "ok",
  "service": "Itinerary Parser API",
  "version": "2.1",
  "capabilities": {
    "pdf": true,
    "zip": true,
    "text": true,
    "json": true
  }
}
```

### POST /api/parse

Parsea un archivo de itinerario y extrae los vuelos.

#### Formatos de entrada soportados

| Content-Type | Descripción |
|--------------|-------------|
| `application/octet-stream` | PDF o ZIP binario |
| `application/zlib` | PDF comprimido con zlib (iOS) |
| `application/json` | JSON con campo `text` |
| `text/plain` | Texto plano |

#### Compresión

Para archivos grandes (>4MB), la app iOS comprime el PDF usando zlib antes de enviarlo:

```swift
// iOS usa COMPRESSION_ZLIB para comprimir
compression_encode_buffer(..., COMPRESSION_ZLIB)
```

El API detecta y descomprime automáticamente.

#### Respuesta exitosa

```json
{
  "success": true,
  "total": 3419,
  "flights": [...],
  "source": "zlib+pdf",
  "textLength": 1234567,
  "metadata": {
    "codigoEmision": "02/26",
    "fechaEmision": "26-ENE-2026",
    "vigenciaInicio": "26-ENE-2026",
    "vigenciaFin": "22-FEB-2026"
  }
}
```

#### Estructura de un vuelo

```json
{
  "status": "A",
  "vuelo": "123",
  "origen": "MEX",
  "salida1": "0600",
  "escala1": "GDL",
  "llegada1": "0730",
  "salida2": "0815",
  "escala2": "",
  "llegada2": "",
  "salida3": "",
  "destino": "LAX",
  "llegada3": "1030",
  "lun": "1",
  "mar": "2",
  "mie": "3",
  "jue": "4",
  "vie": "5",
  "sab": "",
  "dom": "",
  "fechaInicio": "260126",
  "fechaFin": "220226"
}
```

#### Campos de vuelo

| Campo | Descripción |
|-------|-------------|
| `status` | Estado: `A` (nuevo), `C` (cancelado), vacío (sin cambio) |
| `vuelo` | Número de vuelo (sin prefijo AM) |
| `origen` | Código IATA del origen |
| `salida1/2/3` | Hora de salida de cada tramo (HHMM) |
| `escala1/2` | Códigos IATA de escalas |
| `llegada1/2/3` | Hora de llegada de cada tramo (HHMM) |
| `destino` | Código IATA del destino final |
| `lun-dom` | Tipo de equipo por día (1-14) o vacío si no opera |
| `fechaInicio` | Inicio de efectividad (YYMMDD) |
| `fechaFin` | Fin de efectividad (YYMMDD) |

## Extracción de Metadatos

El API extrae automáticamente los metadatos del pie de página del PDF:

```
Emisión 02/26 Del 26 de enero 2026 al 22 de febrero 2026
```

Se convierte a:
- `codigoEmision`: "02/26"
- `vigenciaInicio`: "26-ENE-2026"
- `vigenciaFin`: "22-FEB-2026"

### Limpieza de espacios

El extractor de PDF a veces inserta espacios en números (`202 6` en vez de `2026`). El API limpia estos automáticamente.

## Dependencias

- **pdfplumber**: Extracción precisa de texto de PDFs (preferido)
- **PyPDF2**: Fallback para extracción de PDF

## Límites

| Límite | Valor |
|--------|-------|
| Tamaño máximo de body | 4 MB |
| Timeout | 60 segundos |

Para PDFs mayores a 4MB, la app iOS los comprime con zlib antes de enviar.

## Desarrollo Local

```bash
cd intinerario-api
vercel dev
```

## Despliegue

El proyecto está conectado a GitHub. Cada push a `main` despliega automáticamente en Vercel.

```bash
git add .
git commit -m "Descripción del cambio"
git push origin main
```

## Estructura del Proyecto

```
intinerario-api/
├── api/
│   └── parse.py      # Handler principal
├── requirements.txt  # Dependencias Python
├── vercel.json       # Configuración de Vercel
└── README.md         # Esta documentación
```

## Ejemplos de uso

### cURL - Enviar PDF

```bash
curl -X POST \
  -H "Content-Type: application/octet-stream" \
  --data-binary @itinerario.pdf \
  https://intinerario-api.vercel.app/api/parse
```

### cURL - Enviar texto

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{"text": "1 MEX 0600 GDL 0730 1 2 3 4 5 260126 220226"}' \
  https://intinerario-api.vercel.app/api/parse
```

### Swift (iOS)

```swift
let apiService = ItinerarioAPIService()
let parsed = try await apiService.parseItinerario(from: pdfData, filename: "itinerario.pdf")
print("Vuelos: \(parsed.vuelos.count)")
print("Emisión: \(parsed.codigoEmision)")
```

## Changelog

### v2.1 (2026-01-25)
- Extracción de metadatos solo desde pie de página
- Limpieza de espacios en números del PDF
- Soporte para compresión zlib de iOS

### v2.0
- Soporte para pdfplumber (extracción más precisa)
- Detección mejorada de destinos en vuelos
- Requiere 2 aeropuertos antes de detectar frecuencias

### v1.0
- Versión inicial con PyPDF2
- Soporte para PDF, ZIP y texto
