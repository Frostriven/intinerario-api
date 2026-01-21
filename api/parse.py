from http.server import BaseHTTPRequestHandler
import json
import re
import zipfile
import io
from typing import Optional, List, Dict


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
        return bool(re.match(r'^[0-9]$', token))  # Cambiado: 0-9 en vez de 0-7
    
    @staticmethod
    def is_equipment_code(token: str) -> bool:
        """Verifica si es un código de equipo válido (0-14)"""
        if not re.match(r'^\d{1,2}$', token):
            return False
        return 0 <= int(token) <= 14
    
    def _find_section_boundary(self, tokens: List[str], start_idx: int) -> int:
        i = start_idx
        while i < len(tokens):
            token = tokens[i]
            # Buscar inicio de sección de frecuencias/equipos
            if self.is_equipment_code(token):
                if i > start_idx:
                    lookahead = i
                    freq_count = 0
                    while lookahead < len(tokens) and (
                        self.is_equipment_code(tokens[lookahead]) or 
                        self.is_date(tokens[lookahead])
                    ):
                        if self.is_equipment_code(tokens[lookahead]):
                            freq_count += 1
                        lookahead += 1
                    if freq_count >= 1:  # Cambiado: >= 1 en vez de >= 2
                        return i
            if self.is_date(token):
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
        
        # 2. VUELO
        if re.match(r'^\d+$', tokens[idx]):
            result['vuelo'] = tokens[idx]
            idx += 1
        else:
            return None
        
        if idx >= len(tokens):
            return None
        
        # 3-12. SEGMENTOS DE VUELO
        boundary = self._find_section_boundary(tokens, idx)
        flight_tokens = tokens[idx:boundary]
        
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
        
        # 13-21. FRECUENCIAS Y FECHAS
        day_fields = ['lun', 'mar', 'mie', 'jue', 'vie', 'sab', 'dom']
        day_idx = 0
        dates = []
        
        for token in tokens[boundary:]:
            if self.is_equipment_code(token) and day_idx < 7:
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
                        'EMISIÓN', 'UTC', 'Notas:', 'información', 'AEROMEXICO']
        
        for line in text.split('\n'):
            line = line.strip()
            if not line or re.match(r'^[\s\-]+$', line):
                continue
            if any(p in line for p in skip_patterns):
                continue
            if re.match(r'^\s*\d{1,3}\s*$', line):
                continue
            
            # REGEX CORREGIDO: Solo requiere numero de vuelo + aeropuerto
            if re.match(r'^\s*[AC\-]?\s*\d+\s+[A-Z]{3}', line):
                parsed = self.parse_line(line)
                if parsed and parsed['vuelo']:
                    flights.append(parsed)
        
        return flights


def extract_text_from_zip(zip_data: bytes) -> str:
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
            
            if 'application/json' in content_type:
                data = json.loads(body.decode('utf-8'))
                text = data.get('text', '')
            elif body[:4] == b'PK\x03\x04':
                text = extract_text_from_zip(body)
            else:
                text = body.decode('utf-8', errors='ignore')
            
            parser = ItineraryParser()
            flights = parser.parse_text(text)
            
            response = {'success': True, 'total': len(flights), 'flights': flights}
            
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
        response = {'status': 'ok', 'service': 'Itinerary Parser API', 'version': '2.1'}
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode('utf-8'))
