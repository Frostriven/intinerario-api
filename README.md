# Itinerario Parser API

API serverless en Vercel para parsear itinerarios de Aeroméxico desde archivos PDF.
Usada por **iFly Antigravity 3.0** (app iPad) para importar itinerarios y extraer texto de PDFs con fonts embebidas.

## URL de Producción

```
https://intinerario-api.vercel.app/api/parse
```

## Repositorios

| Repo | URL |
|------|-----|
| **Esta API** | [github.com/Frostriven/intinerario-api](https://github.com/Frostriven/intinerario-api) |
| **iFly (app iPad)** | Repo principal de iFly Antigravity 3.0 |

> Esta API vive como **git submodule** dentro del repo de iFly en `intinerario-api/`.
> Es un proyecto independiente (Python/Vercel) que se despliega por separado.

---

## Estructura del Proyecto

```
intinerario-api/
├── api/
│   ├── parse.py           # Handler principal (serverless function)
│   └── requirements.txt   # Dependencias Python
├── vercel.json            # Configuración de rutas y build de Vercel
└── README.md              # Esta documentación
```

---

## Endpoints

### GET /api/parse

Verifica el estado del servicio.

**Respuesta:**
```json
{
  "status": "ok",
  "service": "Itinerary Parser API",
  "version": "2.2",
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

#### Query Parameters

| Param | Valor | Descripción |
|-------|-------|-------------|
| `mode` | `rawtext` | Devuelve solo el texto extraído del PDF/ZIP como `text/plain`, sin parsear flights. Usado por iFly para el Rol de Servicios cuando PDFKit no puede extraer texto (fonts embebidas NotoSans Type0/Identity-H). |

**Ejemplo rawtext:**
```bash
curl -X POST \
  -H "Content-Type: application/octet-stream" \
  --data-binary @rol.pdf \
  "https://intinerario-api.vercel.app/api/parse?mode=rawtext"
# Respuesta: texto plano extraído del PDF
```

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

El API detecta y descomprime automáticamente: gzip, zlib, y raw deflate (iOS).

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
| `lun-dom` | Tipo de equipo por día (0-14) o vacío si no opera |
| `fechaInicio` | Inicio de efectividad (YYMMDD) |
| `fechaFin` | Fin de efectividad (YYMMDD) |

---

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

El extractor de PDF a veces inserta espacios en números (`202 6` en vez de `2026`). El API limpia estos automáticamente con regex.

---

## Dependencias

| Paquete | Versión | Uso |
|---------|---------|-----|
| **pdfplumber** | >=0.10.0 | Extracción precisa de texto y tablas (preferido) |
| **PyPDF2** | >=3.0.0 | Fallback para extracción de PDF |

## Límites de Vercel

| Límite | Valor |
|--------|-------|
| Tamaño máximo de body | 4 MB |
| Timeout (Hobby plan) | 60 segundos |
| Timeout (Pro plan) | 300 segundos |

Para PDFs mayores a 4MB, la app iOS los comprime con zlib antes de enviar.

---

## Desarrollo Local

### Requisitos previos

- Python 3.9+
- Vercel CLI (`npm i -g vercel`)

### Ejecutar localmente

```bash
cd intinerario-api
pip install -r api/requirements.txt   # Instalar dependencias Python
vercel dev                             # Arranca servidor local en http://localhost:3000
```

### Probar con cURL

```bash
# Health check
curl http://localhost:3000/api/parse

# Parsear PDF
curl -X POST \
  -H "Content-Type: application/octet-stream" \
  --data-binary @itinerario.pdf \
  http://localhost:3000/api/parse

# Extraer solo texto (rawtext)
curl -X POST \
  -H "Content-Type: application/octet-stream" \
  --data-binary @rol.pdf \
  "http://localhost:3000/api/parse?mode=rawtext"

# Enviar JSON con texto
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{"text": "1 MEX 0600 GDL 0730 1 2 3 4 5 260126 220226"}' \
  http://localhost:3000/api/parse
```

---

## Despliegue (Deploy)

### Deploy automático (recomendado)

El proyecto está conectado a GitHub via Vercel. **Cada push a `main` despliega automáticamente.**

```bash
cd intinerario-api
git add .
git commit -m "Descripción del cambio"
git push origin main
# Vercel detecta el push y despliega automáticamente (~30 segundos)
```

### Deploy manual

Si necesitas desplegar sin hacer push:

```bash
cd intinerario-api
vercel --prod          # Despliega directo a producción
```

### Preview deployments

Cada push a una rama que no sea `main` genera un **preview deployment** con URL temporal:

```bash
git checkout -b feature/nueva-funcionalidad
git push origin feature/nueva-funcionalidad
# Vercel genera: https://intinerario-api-xxxx.vercel.app
```

---

## Dashboard de Vercel

Para ver logs, deployments, y configuración:

1. Ir a [vercel.com/dashboard](https://vercel.com/dashboard)
2. Seleccionar el proyecto **intinerario-api**
3. Tabs disponibles:
   - **Deployments**: Historial de deploys, logs de build
   - **Logs**: Runtime logs en tiempo real (print statements de parse.py)
   - **Settings**: Variables de entorno, dominio, etc.
   - **Analytics**: Invocaciones, duración, errores

### Ver logs desde la terminal

```bash
vercel logs https://intinerario-api.vercel.app     # Últimos logs
vercel logs --follow                                # Logs en tiempo real
```

---

## GitHub — Repositorio y Actions

### Acceder al repositorio

```
https://github.com/Frostriven/intinerario-api
```

Desde ahí puedes:
- **Code**: Ver código fuente y commits
- **Pull requests**: Crear/revisar PRs
- **Issues**: Reportar bugs o solicitar features
- **Actions**: Ver workflows de CI/CD (ver abajo)
- **Settings**: Configuración del repo, branch protection, secrets

### GitHub Actions

Actualmente el proyecto **no tiene workflows de GitHub Actions** porque Vercel maneja el CI/CD automáticamente (build + deploy en cada push).

Si quisieras agregar Actions (tests, linting, etc.), crea el archivo:

```
.github/workflows/test.yml
```

Ejemplo básico:

```yaml
name: Tests
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r api/requirements.txt
      - run: python -m pytest tests/ -v
```

Para ver Actions: `https://github.com/Frostriven/intinerario-api/actions`

### Protección de rama (opcional)

En **Settings > Branches** puedes:
- Requerir PR para merges a `main`
- Requerir que Actions pasen antes de merge
- Requerir code reviews

---

## Flujo de trabajo: Actualizar la API desde iFly

Dado que `intinerario-api/` es un submodule dentro de iFly, el flujo es:

```bash
# 1. Editar archivos dentro de intinerario-api/
#    (ej: api/parse.py)

# 2. Commit y push DENTRO del submodule
cd intinerario-api
git add .
git commit -m "Fix: descripción del cambio"
git push origin main
# → Vercel despliega automáticamente

# 3. (Opcional) Actualizar referencia en el repo padre
cd ..   # volver a iFly Antigravity 3.0
git add intinerario-api
git commit -m "Update: intinerario-api submodule"
git push origin main
```

### Clonar el proyecto completo (nueva Mac)

```bash
# Clonar con submodules incluidos
git clone --recurse-submodules <url-repo-ifly>

# O si ya clonaste sin --recurse-submodules:
git submodule update --init --recursive
```

---

## Uso desde Swift (iFly)

```swift
let apiService = ItinerarioAPIService()

// Parsear itinerario completo
let parsed = try await apiService.parseItinerario(from: pdfData, filename: "itinerario.pdf")
print("Vuelos: \(parsed.vuelos.count)")
print("Emisión: \(parsed.codigoEmision)")

// Extraer solo texto (para Rol de Servicios)
let text = try await apiService.extractRawText(from: pdfData)
print("Texto: \(text.prefix(200))")
```

---

## Changelog

### v2.2 (2026-03-26)
- Nuevo query param `mode=rawtext` para extraer solo texto del PDF sin parsear flights
- Usado por iFly como fallback cuando PDFKit no extrae texto de PDFs con fonts embebidas (NotoSans Type0/Identity-H)

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
