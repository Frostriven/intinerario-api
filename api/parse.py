from http.server import BaseHTTPRequestHandler
import json
import re
import zipfile
import gzip
import io
from typing import Optional, List, Dict

# PDF extraction - try multiple libraries
try:
    from PyPDF2 import PdfReader
    HAS_PYPDF2 = True
except ImportError:
    HAS_PYPDF2 = False

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False


def clean_spaced_numbers(text: str) -> str:
    """
    Limpia números con espacios insertados por el PDF extractor.
    Ej: "202 6" -> "2026", "2 6" -> "26"
    """
    # Remover espacios entre dígitos
    return re.sub(r'(\d)\s+(\d)', r'\1\2', text)


def extract_metadata(text: str) -> Dict:
    """
    Extrae metadatos del itinerario.
    Busca en dos formatos:

    Formato 1 (encabezado):
    - EMISIÓN: 01/26 VIGENCIA: 29-DIC-2025 al 25-ENE-2026
    - FECHA: 23-DIC-2025

    Formato 2 (pie de página):
    - Emisión 02/26 Del 26 de enero 2026 al 22 de febrero 2026.
    """
    metadata = {
        'codigoEmision': '',
        'fechaEmision': '',
        'vigenciaInicio': '',
        'vigenciaFin': ''
    }

    # Limpiar espacios en números del texto
    clean_text = clean_spaced_numbers(text)

    # ========== FORMATO 1: Encabezado ==========
    # Solo buscar en las primeras líneas
    header_lines = clean_text[:3000]

    # Buscar EMISIÓN: XX/XX
    emision_match = re.search(r'EMISI[OÓ]N[:\s]+(\d{2}/\d{2})', header_lines, re.IGNORECASE)
    if emision_match:
        metadata['codigoEmision'] = emision_match.group(1)

    # Buscar VIGENCIA: DD-MMM-YYYY al DD-MMM-YYYY
    vigencia_match = re.search(
        r'VIGENCIA[:\s]+(\d{1,2}-[A-Z]{3}-\d{4})\s+al\s+(\d{1,2}-[A-Z]{3}-\d{4})',
        header_lines, re.IGNORECASE
    )
    if vigencia_match:
        metadata['vigenciaInicio'] = vigencia_match.group(1).upper()
        metadata['vigenciaFin'] = vigencia_match.group(2).upper()

    # Buscar FECHA: DD-MMM-YYYY
    fecha_match = re.search(r'FECHA[:\s]+(\d{1,2}-[A-Z]{3}-\d{4})', header_lines, re.IGNORECASE)
    if fecha_match:
        metadata['fechaEmision'] = fecha_match.group(1).upper()

    # ========== FORMATO 2: Pie de página ==========
    # Si no encontramos código de emisión, buscar en formato alternativo
    # "Emisión 02/26 Del 26 de enero 2026 al 22 de febrero 2026"
    if not metadata['codigoEmision']:
        # Buscar en todo el texto (puede estar en cualquier página)
        footer_match = re.search(
            r'Emisi[oó]n\s+(\d{2}/\d{2})\s+Del\s+(\d{1,2})\s+de\s+([a-zA-Z]+)\s+(\d{4})\s+al\s+(\d{1,2})\s+de\s+([a-zA-Z]+)\s+(\d{4})',
            clean_text, re.IGNORECASE
        )
        if footer_match:
            metadata['codigoEmision'] = footer_match.group(1)

            # Convertir mes en español a formato corto
            month_map = {
                'enero': 'ENE', 'febrero': 'FEB', 'marzo': 'MAR', 'abril': 'ABR',
                'mayo': 'MAY', 'junio': 'JUN', 'julio': 'JUL', 'agosto': 'AGO',
                'septiembre': 'SEP', 'octubre': 'OCT', 'noviembre': 'NOV', 'diciembre': 'DIC'
            }

            # Fecha inicio: "26 de enero 2026" -> "26-ENE-2026"
            day_start = footer_match.group(2)
            month_start = month_map.get(footer_match.group(3).lower(), footer_match.group(3)[:3].upper())
            year_start = footer_match.group(4)
            metadata['vigenciaInicio'] = f"{day_start}-{month_start}-{year_start}"

            # Fecha fin: "22 de febrero 2026" -> "22-FEB-2026"
            day_end = footer_match.group(5)
            month_end = month_map.get(footer_match.group(6).lower(), footer_match.group(6)[:3].upper())
            year_end = footer_match.group(7)
            metadata['vigenciaFin'] = f"{day_end}-{month_end}-{year_end}"

            # Usar fecha de inicio como fecha de emisión si no tenemos otra
            if not metadata['fechaEmision']:
                metadata['fechaEmision'] = metadata['vigenciaInicio']

    return metadata


class ItineraryParser:
    COLUMN_NAMES = [
        'status', 'vuelo', 'origen', 'salida1', 'escala1', 'llegada1',
        'salida2', 'escala2', 'llegada2', 'salida3', 'destino', 'llegada3',
        'lun', 'mar', 'mie', 'jue', 'vie', 'sab', 'dom',
        'fechaInicio', 'fechaFin'
    ]

    @staticmethod
    def is_airport(token: str) -> bool:
        return bool(re.match(r'^[A-Z]{3}$', token))

    @staticmethod
    def is_time(token: str) -> bool:
        if not re.match(r'^\d{2,4}$', token):
            return False
        return int(token) <= 2359

    @staticmethod
    def is_date(token: str) -> bool:
        if not re.match(r'^\d{6}$', token):
            return False
        mm, dd = int(token[2:4]), int(token[4:6])
        return 1 <= mm <= 12 and 1 <= dd <= 31

    @staticmethod
    def is_frequency(token: str) -> bool:
        return bool(re.match(r'^[0-7]$', token))

    def _find_section_boundary(self, tokens: List[str], start_idx: int) -> int:
        """
        Encuentra el límite entre la sección de segmentos de vuelo y la sección de frecuencias/fechas.
        MEJORADO: Requiere al menos 2 aeropuertos antes de considerar frecuencias.
        """
        i = start_idx
        airport_count = 0

        while i < len(tokens):
            token = tokens[i]

            # Contar aeropuertos encontrados
            if self.is_airport(token):
                airport_count += 1
            # También contar si es un token concatenado número+aeropuerto
            elif re.match(r'^(\d{2,4})([A-Z]{3})$', token):
                airport_count += 1

            # Solo buscar frecuencias si ya encontramos al menos 2 aeropuertos
            # (origen + destino mínimo)
            if self.is_frequency(token) and airport_count >= 2:
                if i > start_idx:
                    lookahead = i
                    freq_count = 0
                    while lookahead < len(tokens) and (
                        self.is_frequency(tokens[lookahead]) or
                        self.is_date(tokens[lookahead])
                    ):
                        if self.is_frequency(tokens[lookahead]):
                            freq_count += 1
                        lookahead += 1
                    if freq_count >= 2:
                        return i
                else:
                    return i

            # Si encontramos una fecha Y ya tenemos al menos 2 aeropuertos, parar
            if self.is_date(token) and airport_count >= 2:
                return i

            i += 1

        return len(tokens)

    def parse_line(self, line: str) -> Optional[Dict]:
        tokens = line.split()
        if len(tokens) < 4:
            return None

        result = {col: '' for col in self.COLUMN_NAMES}
        idx = 0

        # 1. STATUS
        if tokens[idx] in ['A', 'C', '-']:
            if tokens[idx] in ['A', 'C']:
                result['status'] = tokens[idx]
            idx += 1

        if idx >= len(tokens):
            return None

        # 2. VUELO - handle both "1 MEX" and "1MEX" formats
        token = tokens[idx]
        if re.match(r'^\d+$', token):
            # Format: "1" "MEX" (separate tokens)
            result['vuelo'] = token
            idx += 1
        elif re.match(r'^(\d+)([A-Z]{3})$', token):
            # Format: "1MEX" (concatenated - pdfplumber format)
            match = re.match(r'^(\d+)([A-Z]{3})$', token)
            result['vuelo'] = match.group(1)
            # Insert the airport back as a pseudo-token for segment parsing
            tokens = tokens[:idx] + [match.group(1), match.group(2)] + tokens[idx+1:]
            idx += 1
        else:
            return None

        if idx >= len(tokens):
            return None

        # 3-12. SEGMENTOS DE VUELO
        boundary = self._find_section_boundary(tokens, idx)
        flight_tokens = tokens[idx:boundary]

        # Pre-process flight_tokens to split concatenated time+airport tokens
        # e.g., "10MAD" -> "10", "MAD" or "1030MEX" -> "1030", "MEX"
        expanded_tokens = []
        for token in flight_tokens:
            # Check if token is time+airport concatenated (e.g., "1030MAD", "955MEX")
            concat_match = re.match(r'^(\d{2,4})([A-Z]{3})$', token)
            if concat_match:
                time_part = concat_match.group(1)
                airport_part = concat_match.group(2)
                if int(time_part) <= 2359:  # Valid time
                    expanded_tokens.append(time_part)
                    expanded_tokens.append(airport_part)
                else:
                    expanded_tokens.append(token)
            else:
                expanded_tokens.append(token)

        flight_tokens = expanded_tokens
        segments = []
        seg_idx = 0

        while seg_idx < len(flight_tokens):
            token = flight_tokens[seg_idx]
            if self.is_airport(token):
                segment = {'airport': token, 'times': []}
                seg_idx += 1
                while seg_idx < len(flight_tokens) and self.is_time(flight_tokens[seg_idx]):
                    segment['times'].append(flight_tokens[seg_idx])
                    seg_idx += 1
                segments.append(segment)
            else:
                seg_idx += 1

        # DEBUG: Log problematic lines with only 1 segment
        if len(segments) == 1 and result.get('vuelo'):
            import sys
            print(f"[DEBUG] Vuelo {result['vuelo']} con solo 1 segmento:", file=sys.stderr)
            print(f"  Line: {line[:100]}...", file=sys.stderr)
            print(f"  Tokens: {tokens}", file=sys.stderr)
            print(f"  Boundary: {boundary}, flight_tokens: {flight_tokens}", file=sys.stderr)
            print(f"  Segments: {segments}", file=sys.stderr)

        if len(segments) >= 1:
            result['origen'] = segments[0]['airport']
            if segments[0]['times']:
                result['salida1'] = segments[0]['times'][0]

        if len(segments) >= 2:
            result['escala1'] = segments[1]['airport']
            if len(segments[1]['times']) >= 1:
                result['llegada1'] = segments[1]['times'][0]
            if len(segments[1]['times']) >= 2:
                result['salida2'] = segments[1]['times'][1]

        if len(segments) >= 3:
            result['escala2'] = segments[2]['airport']
            if len(segments[2]['times']) >= 1:
                result['llegada2'] = segments[2]['times'][0]
            if len(segments[2]['times']) >= 2:
                result['salida3'] = segments[2]['times'][1]

        if len(segments) >= 4:
            result['destino'] = segments[3]['airport']
            if segments[3]['times']:
                result['llegada3'] = segments[3]['times'][0]

        # FIX: Si solo hay 1 segmento, buscar el destino en los tokens después del boundary
        # Algunos vuelos tienen el destino mezclado con los días de frecuencia
        if len(segments) == 1 and result.get('origen'):
            # Buscar un aeropuerto en los tokens restantes (antes de las fechas)
            remaining_tokens = tokens[boundary:]
            for i, token in enumerate(remaining_tokens):
                if self.is_airport(token):
                    # Encontrado un aeropuerto - usarlo como destino
                    result['escala1'] = token
                    # Buscar tiempo de llegada antes de este aeropuerto
                    if i > 0 and self.is_time(remaining_tokens[i-1]):
                        result['llegada1'] = remaining_tokens[i-1]
                    break

        # 13-21. FRECUENCIAS Y FECHAS
        day_fields = ['lun', 'mar', 'mie', 'jue', 'vie', 'sab', 'dom']
        day_idx = 0
        dates = []

        for token in tokens[boundary:]:
            if self.is_frequency(token) and day_idx < 7:
                result[day_fields[day_idx]] = token
                day_idx += 1
            elif self.is_date(token):
                dates.append(token)

        if len(dates) >= 1:
            result['fechaInicio'] = dates[0]
        if len(dates) >= 2:
            result['fechaFin'] = dates[1]

        return result

    def parse_text(self, text: str) -> List[Dict]:
        flights = []
        skip_patterns = ['S VLO', 'EFECTIVIDAD', 'ITINERARIOS', 'Emisión',
                        'EMISIÓN', 'UTC', 'Notas:', 'información']

        for line in text.split('\n'):
            line = line.strip()
            if not line or re.match(r'^[\s\-]+$', line):
                continue
            if any(p in line for p in skip_patterns):
                continue
            if re.match(r'^\s*\d{1,3}\s*$', line):
                continue
            # Match both formats:
            # - "1 MEX 10" (PDFKit format with spaces)
            # - "1MEX 10MAD" (pdfplumber format, concatenated)
            if re.match(r'^\s*[AC\-]?\s*\d+\s*[A-Z]{3}\s+\d+', line):
                parsed = self.parse_line(line)
                if parsed and parsed['vuelo']:
                    flights.append(parsed)

        return flights


def extract_text_from_zip(zip_data: bytes) -> str:
    """Extract text from ZIP file containing TXT files"""
    all_text = []
    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        txt_files = sorted(
            [f for f in zf.namelist() if f.endswith('.txt')],
            key=lambda x: int(re.search(r'\d+', x).group()) if re.search(r'\d+', x) else 0
        )
        for txt_file in txt_files:
            content = zf.read(txt_file).decode('utf-8', errors='ignore')
            all_text.append(content)
    return '\n'.join(all_text)


def extract_text_from_pdf(pdf_data: bytes) -> str:
    """Extract text from PDF using available library"""
    # Try pdfplumber first (better text extraction)
    if HAS_PDFPLUMBER:
        try:
            all_text = []
            with pdfplumber.open(io.BytesIO(pdf_data)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        all_text.append(text)
            return '\n'.join(all_text)
        except Exception:
            pass  # Fall through to PyPDF2

    # Try PyPDF2
    if HAS_PYPDF2:
        try:
            all_text = []
            reader = PdfReader(io.BytesIO(pdf_data))
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    all_text.append(text)
            return '\n'.join(all_text)
        except Exception:
            pass

    raise ValueError("No PDF library available or PDF extraction failed")


def is_pdf(data: bytes) -> bool:
    """Check if data is a PDF file"""
    return data[:4] == b'%PDF'


def is_zip(data: bytes) -> bool:
    """Check if data is a ZIP file"""
    return data[:4] == b'PK\x03\x04'


def is_gzip(data: bytes) -> bool:
    """Check if data is gzip compressed"""
    return data[:2] == b'\x1f\x8b'


def is_zlib(data: bytes) -> bool:
    """Check if data is zlib compressed (CMF byte check)"""
    if len(data) < 2:
        return False
    # zlib header: CMF (usually 0x78) + FLG
    cmf = data[0]
    flg = data[1]
    # Check if CMF indicates deflate compression
    if cmf & 0x0F == 8:  # Compression method 8 = deflate
        # Verify checksum: (CMF * 256 + FLG) % 31 == 0
        if (cmf * 256 + flg) % 31 == 0:
            return True
    return False


def is_raw_deflate(content_type: str) -> bool:
    """Check if Content-Type indicates raw deflate (iOS Compression framework format)"""
    # iOS's COMPRESSION_ZLIB produces raw deflate without zlib header
    # We detect this by Content-Type header
    return 'application/zlib' in content_type or 'application/deflate' in content_type


def decompress_gzip(data: bytes) -> bytes:
    """Decompress gzip data"""
    return gzip.decompress(data)


def decompress_zlib(data: bytes) -> bytes:
    """Decompress zlib data"""
    import zlib
    return zlib.decompress(data)


def decompress_raw_deflate(data: bytes) -> bytes:
    """Decompress raw deflate data (iOS Compression framework format)"""
    import zlib
    # wbits=-15 tells zlib to expect raw deflate without header
    return zlib.decompress(data, wbits=-15)


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            content_type = self.headers.get('Content-Type', '')

            text = ''
            source_type = 'unknown'

            # Check if data is compressed and decompress
            if is_gzip(body):
                body = decompress_gzip(body)
                source_type = 'gzip+'
            elif is_zlib(body):
                body = decompress_zlib(body)
                source_type = 'zlib+'
            elif 'application/zlib' in content_type or 'application/deflate' in content_type:
                # iOS Compression framework sends raw deflate with this Content-Type
                body = decompress_raw_deflate(body)
                source_type = 'deflate+'

            # Determine input type and extract text
            compressed_prefix = source_type if source_type.endswith('+') else ''

            if 'application/json' in content_type and not compressed_prefix:
                # JSON with text field
                data = json.loads(body.decode('utf-8'))
                text = data.get('text', '')
                source_type = 'json'
            elif is_pdf(body):
                # PDF file - extract text
                text = extract_text_from_pdf(body)
                source_type = compressed_prefix + 'pdf'
            elif is_zip(body):
                # ZIP file with TXT files
                text = extract_text_from_zip(body)
                source_type = compressed_prefix + 'zip'
            else:
                # Plain text
                text = body.decode('utf-8', errors='ignore')
                source_type = compressed_prefix + 'text' if compressed_prefix else 'text'

            # Extract metadata from header
            metadata = extract_metadata(text)

            # Parse the text
            parser = ItineraryParser()
            flights = parser.parse_text(text)

            response = {
                'success': True,
                'total': len(flights),
                'flights': flights,
                'source': source_type,
                'textLength': len(text),
                'metadata': metadata
            }

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(response, ensure_ascii=False).encode('utf-8'))

        except Exception as e:
            response = {'success': False, 'error': str(e), 'total': 0, 'flights': []}
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode('utf-8'))

    def do_GET(self):
        response = {
            'status': 'ok',
            'service': 'Itinerary Parser API',
            'version': '2.1',
            'capabilities': {
                'pdf': HAS_PYPDF2 or HAS_PDFPLUMBER,
                'zip': True,
                'text': True,
                'json': True
            }
        }
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode('utf-8'))
