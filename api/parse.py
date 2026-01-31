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
    Extrae metadatos del itinerario usando SOLO el formato del pie de página.
    Formato: "Emisión 02/26 Del 26 de enero 2026 al 22 de febrero 2026"

    Este formato es más confiable porque está presente en todas las páginas.
    """
    metadata = {
        'codigoEmision': '',
        'fechaEmision': '',
        'vigenciaInicio': '',
        'vigenciaFin': ''
    }

    # Limpiar espacios en números del texto
    clean_text = clean_spaced_numbers(text)

    # Buscar formato de pie de página:
    # "Emisión 02/26 Del 26 de enero 2026 al 22 de febrero 2026"
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

        # Usar fecha de inicio como fecha de emisión
        metadata['fechaEmision'] = metadata['vigenciaInicio']

    return metadata


class ItineraryParser:
    COLUMN_NAMES = [
        'status', 'vuelo', 'origen', 'salida1', 'escala1', 'llegada1',
        'salida2', 'escala2', 'llegada2', 'salida3', 'destino', 'llegada3',
        'lun', 'mar', 'mie', 'jue', 'vie', 'sab', 'dom',
        'fechaInicio', 'fechaFin'
    ]

    def __init__(self):
        # Posiciones de columna de los días (se calibran con el encabezado)
        self.day_column_positions = None

    @staticmethod
    def is_airport(token: str) -> bool:
        return bool(re.match(r'^[A-Z]{3}$', token))

    @staticmethod
    def is_time(token: str) -> bool:
        # Aceptar 1-4 dígitos (ej: "5" = 00:05, "10" = 00:10, "1030" = 10:30)
        if not re.match(r'^\d{1,4}$', token):
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
        # Códigos de equipo: 0-8 (un dígito) o 10-14 (dos dígitos)
        if re.match(r'^[0-8]$', token):
            return True
        if token in ['10', '11', '12', '13', '14']:
            return True
        return False

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
            # También contar si es un token concatenado número+aeropuerto (ej: "5MAD", "1030MAD")
            elif re.match(r'^(\d{1,4})([A-Z]{3})$', token):
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
            # Check if token is time+airport concatenated (e.g., "1030MAD", "955MEX", "5MAD")
            concat_match = re.match(r'^(\d{1,4})([A-Z]{3})$', token)
            if concat_match:
                time_part = concat_match.group(1)
                airport_part = concat_match.group(2)
                if int(time_part) <= 2359:  # Valid time (1-4 digits)
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
        # NUEVO: Parsear desde el final de la línea (más confiable)
        day_fields = ['lun', 'mar', 'mie', 'jue', 'vie', 'sab', 'dom']

        # Recolectar todos los tokens restantes
        remaining_tokens = tokens[boundary:]

        # Buscar fechas desde el final (son números de 6 dígitos)
        dates = []
        date_indices = []
        for i, token in enumerate(remaining_tokens):
            if self.is_date(token):
                dates.append(token)
                date_indices.append(i)

        # Encontrar dónde terminan los códigos de día
        # Los códigos están ANTES de las fechas
        if date_indices:
            first_date_idx = date_indices[0]
            # Los 7 tokens antes de la primera fecha son los días (si existen)
            day_tokens_start = max(0, first_date_idx - 7)
            day_tokens = remaining_tokens[day_tokens_start:first_date_idx]
        else:
            # No hay fechas, tomar los últimos tokens como días
            day_tokens = remaining_tokens[-7:] if len(remaining_tokens) >= 7 else remaining_tokens

        # Asignar códigos de equipo a días
        # Si tenemos exactamente 7, asignar en orden
        # Si tenemos menos, alinear a la DERECHA (hacia domingo)
        valid_day_codes = []
        for token in day_tokens:
            if self.is_frequency(token):
                valid_day_codes.append(token)

        if len(valid_day_codes) == 7:
            # 7 códigos = todos los días en orden
            for i, code in enumerate(valid_day_codes):
                # Solo guardar si NO es vacío (celda vacía = no opera)
                if code and code not in ['', '-']:
                    result[day_fields[i]] = code
        elif len(valid_day_codes) > 0:
            # Menos de 7: alinear a la derecha (domingo es el último)
            start_idx = 7 - len(valid_day_codes)
            for i, code in enumerate(valid_day_codes):
                day_idx = start_idx + i
                if 0 <= day_idx < 7 and code and code not in ['', '-']:
                    result[day_fields[day_idx]] = code

        if len(dates) >= 1:
            result['fechaInicio'] = dates[0]
        if len(dates) >= 2:
            result['fechaFin'] = dates[1]

        return result

    def parse_text(self, text: str) -> List[Dict]:
        flights = []
        skip_patterns = ['S VLO', 'EFECTIVIDAD', 'ITINERARIOS', 'Emisión',
                        'EMISIÓN', 'UTC', 'Notas:', 'información']

        lines = text.split('\n')

        # Primero, buscar el encabezado para calibrar posiciones de columna
        self._calibrate_day_columns(lines)

        for line in lines:
            original_line = line  # Guardar línea original con espacios
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
                parsed = self.parse_line(original_line)
                if parsed and parsed['vuelo']:
                    flights.append(parsed)

        return flights

    def _calibrate_day_columns(self, lines: List[str]):
        """
        Busca el encabezado 'L M M J V S D' y extrae las posiciones de cada columna.
        Esto permite mapear correctamente las frecuencias a los días.
        """
        day_letters = ['L', 'M', 'M', 'J', 'V', 'S', 'D']

        for line in lines:
            # Buscar línea que contenga el patrón de días
            # Puede ser "L M M J V S D" o similar
            if re.search(r'\bL\s+M\s+M\s+J\s+V\s+S\s+D\b', line):
                positions = []
                # Encontrar posición de cada letra de día
                idx = 0
                for day in day_letters:
                    pos = line.find(day, idx)
                    if pos != -1:
                        positions.append(pos)
                        idx = pos + 1
                    else:
                        positions.append(-1)

                if len(positions) == 7 and all(p >= 0 for p in positions):
                    self.day_column_positions = positions
                    return

        # Si no encontramos el encabezado, usar posiciones por defecto
        self.day_column_positions = None

    def _assign_frequencies_by_position(self, line: str, day_fields: List[str], expected_frequencies: List[str]) -> Dict[str, str]:
        """
        Asigna códigos de equipo a días revisando directamente cada columna.
        Solo devuelve resultados si la cantidad encontrada coincide con expected_frequencies.
        """
        result = {}

        if not self.day_column_positions:
            return result

        found_codes = []

        # Para cada día, buscar código de equipo en su columna
        for day_idx, day_pos in enumerate(self.day_column_positions):
            # Buscar SOLO en la posición exacta (±1 máximo)
            for offset in [0, -1, 1]:
                pos = day_pos + offset
                if pos < 0 or pos >= len(line):
                    continue

                char = line[pos]

                # Verificar que sea un dígito
                if char.isdigit():
                    prev_char = line[pos - 1] if pos > 0 else ' '
                    next_char = line[pos + 1] if pos < len(line) - 1 else ' '

                    # Verificar si es un número de dos dígitos (10-14)
                    if char == '1' and next_char.isdigit() and not prev_char.isdigit():
                        two_digit = char + next_char
                        if two_digit in ['10', '11', '12', '13', '14']:
                            found_codes.append((day_fields[day_idx], two_digit))
                            break
                    # Si es un dígito aislado (no parte de un número mayor)
                    elif not prev_char.isdigit() and not next_char.isdigit():
                        found_codes.append((day_fields[day_idx], char))
                        break

        # Validar: solo usar si encontramos aprox la misma cantidad que los tokens
        # Permitir margen de ±1 por posibles diferencias de extracción
        if abs(len(found_codes) - len(expected_frequencies)) <= 1:
            for day, code in found_codes:
                result[day] = code
        else:
            # Fallback: usar tokens con alineación a la derecha
            start_idx = 7 - len(expected_frequencies)
            for i, freq in enumerate(expected_frequencies):
                day_idx = start_idx + i
                if 0 <= day_idx < 7:
                    result[day_fields[day_idx]] = freq

        return result


def parse_table_row(row: List[str], day_fields: List[str]) -> Optional[Dict]:
    """
    Parsea una fila de tabla extraída por pdfplumber.
    La tabla tiene columnas fijas que mapean directamente a los campos.
    """
    if not row or len(row) < 10:
        return None

    # Limpiar celdas vacías/None
    row = [str(cell).strip() if cell else '' for cell in row]

    # Estructura esperada de columnas:
    # [STATUS, VLO, ORIGEN, SALIDA, ESCALA/DESTINO, LLEGADA, ..., L, M, M, J, V, S, D, INICIO, FIN]
    # El número exacto de columnas puede variar según escalas

    result = {
        'status': '', 'vuelo': '', 'origen': '', 'salida1': '',
        'escala1': '', 'llegada1': '', 'salida2': '', 'escala2': '',
        'llegada2': '', 'salida3': '', 'destino': '', 'llegada3': '',
        'lun': '', 'mar': '', 'mie': '', 'jue': '', 'vie': '', 'sab': '', 'dom': '',
        'fechaInicio': '', 'fechaFin': ''
    }

    # Buscar índice donde empiezan los días (buscar patrón de 7 celdas con dígitos 0-14)
    day_start_idx = -1
    for i in range(len(row) - 8):  # Necesitamos al menos 7 celdas para días + fechas
        # Verificar si las siguientes 7 celdas parecen códigos de equipo
        potential_days = row[i:i+7]
        valid_codes = 0
        for cell in potential_days:
            if cell in ['0', '1', '2', '3', '4', '5', '6', '7', '8', '10', '11', '12', '13', '14', '']:
                valid_codes += 1
        if valid_codes >= 5:  # Al menos 5 de 7 parecen códigos válidos
            day_start_idx = i
            break

    if day_start_idx == -1:
        return None  # No encontramos la sección de días

    # Extraer datos del vuelo (antes de los días)
    flight_data = row[:day_start_idx]

    # Status y número de vuelo
    idx = 0
    if len(flight_data) > idx and flight_data[idx] in ['A', 'C', '-', '']:
        if flight_data[idx] in ['A', 'C']:
            result['status'] = flight_data[idx]
        idx += 1

    if len(flight_data) > idx and flight_data[idx]:
        # Puede ser solo número o número+origen concatenado
        vuelo_cell = flight_data[idx]
        match = re.match(r'^(\d+)([A-Z]{3})?$', vuelo_cell)
        if match:
            result['vuelo'] = match.group(1)
            if match.group(2):
                result['origen'] = match.group(2)
                idx += 1
            else:
                idx += 1
                if len(flight_data) > idx:
                    result['origen'] = flight_data[idx]
                    idx += 1
        else:
            return None  # No es un vuelo válido

    # Parsear segmentos restantes (origen, salida, escalas, llegadas)
    segments = flight_data[idx:]
    seg_idx = 0

    # Origen (si no lo tenemos)
    if not result['origen'] and seg_idx < len(segments):
        if re.match(r'^[A-Z]{3}$', segments[seg_idx]):
            result['origen'] = segments[seg_idx]
            seg_idx += 1

    # Salida 1
    if seg_idx < len(segments) and re.match(r'^\d{1,4}$', segments[seg_idx]):
        result['salida1'] = segments[seg_idx]
        seg_idx += 1

    # Escala 1 / Destino
    if seg_idx < len(segments) and re.match(r'^[A-Z]{3}$', segments[seg_idx]):
        result['escala1'] = segments[seg_idx]
        seg_idx += 1

    # Llegada 1
    if seg_idx < len(segments) and re.match(r'^\d{1,4}$', segments[seg_idx]):
        result['llegada1'] = segments[seg_idx]
        seg_idx += 1

    # Más segmentos si existen...
    if seg_idx < len(segments) and re.match(r'^\d{1,4}$', segments[seg_idx]):
        result['salida2'] = segments[seg_idx]
        seg_idx += 1

    if seg_idx < len(segments) and re.match(r'^[A-Z]{3}$', segments[seg_idx]):
        result['escala2'] = segments[seg_idx]
        seg_idx += 1

    if seg_idx < len(segments) and re.match(r'^\d{1,4}$', segments[seg_idx]):
        result['llegada2'] = segments[seg_idx]
        seg_idx += 1

    # Extraer días (7 columnas a partir de day_start_idx)
    for i, day_field in enumerate(day_fields):
        if day_start_idx + i < len(row):
            code = row[day_start_idx + i]
            # Solo guardar si es un código válido (no vacío, no "-1")
            if code and code not in ['', '-1', '-']:
                result[day_field] = code

    # Extraer fechas (después de los días)
    date_start_idx = day_start_idx + 7
    if date_start_idx < len(row) and row[date_start_idx]:
        fecha = row[date_start_idx]
        if re.match(r'^\d{6}$', fecha):
            result['fechaInicio'] = fecha
    if date_start_idx + 1 < len(row) and row[date_start_idx + 1]:
        fecha = row[date_start_idx + 1]
        if re.match(r'^\d{6}$', fecha):
            result['fechaFin'] = fecha

    # Validar que tengamos datos mínimos
    if result['vuelo'] and result['origen']:
        return result
    return None


def extract_flights_from_pdf_tables(pdf_data: bytes) -> List[Dict]:
    """
    Extrae vuelos usando extracción de tablas de pdfplumber.
    Esto preserva la estructura de columnas correctamente.
    """
    if not HAS_PDFPLUMBER:
        return []

    flights = []
    day_fields = ['lun', 'mar', 'mie', 'jue', 'vie', 'sab', 'dom']

    try:
        with pdfplumber.open(io.BytesIO(pdf_data)) as pdf:
            for page in pdf.pages:
                # Extraer tablas de la página
                tables = page.extract_tables()

                for table in tables:
                    if not table:
                        continue

                    for row in table:
                        if not row:
                            continue

                        # Intentar parsear la fila como un vuelo
                        parsed = parse_table_row(row, day_fields)
                        if parsed:
                            flights.append(parsed)

    except Exception as e:
        import sys
        print(f"[ERROR] Table extraction failed: {e}", file=sys.stderr)
        return []

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
                # PDF file - intentar extracción de tablas primero
                table_flights = extract_flights_from_pdf_tables(body)
                if table_flights and len(table_flights) > 0:
                    # Éxito con tablas - usar estos vuelos directamente
                    text = extract_text_from_pdf(body)  # Para metadatos
                    source_type = compressed_prefix + 'pdf-table'

                    metadata = extract_metadata(text)

                    response = {
                        'success': True,
                        'total': len(table_flights),
                        'flights': table_flights,
                        'source': source_type,
                        'textLength': len(text),
                        'metadata': metadata
                    }

                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps(response, ensure_ascii=False).encode('utf-8'))
                    return

                # Fallback a extracción de texto
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
