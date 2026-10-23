import arcpy
import os
import math
import re
import sys
import traceback

try:
    import arcpy.mp as arc_mapping
    IS_PRO = True
except ImportError:
    import arcpy.mapping as arc_mapping
    IS_PRO = False
# ----------------------

class Toolbox(object):
    def __init__(self):
        self.label = "Toolkit Batas Desa"
        self.alias = "batas_desa_toolkit"
        # Daftar 7 tool yang akan muncul berurutan di ArcGIS
        self.tools = [
            GenerateTK, 
            AutoNameTKBiasa, 
            QCTitikKartometrik, 
            ExportTKExcel, 
            ExportLaporanTK,
            TopologiBatasDesa,
            ValidasiTopologiDesa,
            BatchExportMXD
        ]

# =========================================================
# TOOL 1: GENERATE TK SIMPUL (PERBAIKAN DISTORSI & KEYERROR)
# =========================================================
class GenerateTK(object):
    def __init__(self):
        self.label = "1. Generate TK Simpul"
        self.description = "Mengekstrak Titik Kartometrik dari pertemuan batas poligon."
        self.canRunInBackground = False
    
    def getParameterInfo(self):
        in_fc = arcpy.Parameter(
            displayName="Input Feature Class (Batas Administrasi)",
            name="in_fc",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input")
        in_fc.filter.list = ["Polygon"]

        kdepum_field = arcpy.Parameter(
            displayName="Field Kode Desa/Unsur (KDEPUM)",
            name="kdepum_field",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        kdepum_field.parameterDependencies = [in_fc.name]

        out_fc = arcpy.Parameter(
            displayName="Output Feature Class (Titik Kartometrik)",
            name="out_fc",
            datatype="DEFeatureClass",
            parameterType="Required",
            direction="Output")

        params = [in_fc, kdepum_field, out_fc]
        return params

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        return

    def dd_to_dms(self, dd, is_lat):
        direction = ""
        if is_lat:
            direction = "N" if dd >= 0 else "S"
        else:
            direction = "E" if dd >= 0 else "W"
        
        dd = abs(dd)
        degrees = int(dd)
        minutes = int((dd - degrees) * 60)
        seconds = (((dd - degrees) * 60) - minutes) * 60
        
        # Cegah detik melampaui 59.999 (misal pembulatan 59.9998 jadi 60.000)
        if round(seconds, 3) >= 60.0:
            seconds = 0.0
            minutes += 1
        if minutes >= 60:
            minutes = 0
            degrees += 1
            
        # Format string detik: 2 digit di depan koma, 3 digit di belakang koma, ganti titik jadi koma
        sec_str = u"{0:06.3f}".format(seconds).replace(u".", u",")
        
        return u"{0}\u00B0 {1}' {2}\" {3}".format(degrees, minutes, sec_str, direction)

    def execute(self, parameters, messages):
        in_fc = parameters[0].valueAsText
        kdepum_field = parameters[1].valueAsText
        out_fc = parameters[2].valueAsText

        arcpy.env.overwriteOutput = True
        desc = arcpy.Describe(in_fc)
        sr = desc.spatialReference
        
        base_gcs = sr if sr.type == "Geographic" else sr.GCS
        scratch = arcpy.env.scratchGDB

        arcpy.AddMessage("1. Mempersiapkan data dan merapatkan celah (Topology Fix)...")
        
        if sr.type == "Geographic":
            ext = desc.extent
            cen_x = (ext.XMin + ext.XMax) / 2.0
            utm_zone = int((cen_x + 180) / 6) + 1
            epsg = 32700 + utm_zone if ext.YMin < 0 else 32600 + utm_zone
            work_sr = arcpy.SpatialReference(epsg)
            
            arcpy.AddMessage("   -> Menyalin sementara data untuk memutus ikatan Topologi...")
            temp_copy = os.path.join(scratch, "temp_tk_poly_copy")
            arcpy.CopyFeatures_management(in_fc, temp_copy)
            
            arcpy.AddMessage("   -> Proyeksi on-the-fly ke UTM Zone {0}...".format(utm_zone))
            work_fc = os.path.join(scratch, "temp_tk_poly_utm")
            arcpy.Project_management(temp_copy, work_fc, work_sr)
            
            try:
                arcpy.Delete_management(temp_copy)
            except:
                pass
        else:
            work_fc = os.path.join(scratch, "temp_tk_poly")
            work_sr = sr
            arcpy.CopyFeatures_management(in_fc, work_fc)

        # PERBAIKAN DISTORSI: Mengubah dari 3 Meters menjadi 0.05 Meters (5 Centimeter)
        arcpy.AddMessage("   -> Menyatukan poligon yang renggang (Toleransi 5 Centimeter)...")
        arcpy.Integrate_management(work_fc, "0.05 Meters")

        arcpy.AddMessage("2. Membaca Topologi Batas dan Identitas Desa...")
        vertex_dict = {}
        geom_dict = {}
        
        with arcpy.da.SearchCursor(work_fc, ['SHAPE@', kdepum_field]) as cursor:
            for row in cursor:
                geom = row[0]
                val = row[1]
                if not geom or val is None or str(val).strip() == "": 
                    continue
                    
                kdepum = str(val).strip()
                for part in geom:
                    for pnt in part:
                        if pnt:
                            coord = (round(pnt.X, 2), round(pnt.Y, 2))
                            if coord not in vertex_dict:
                                vertex_dict[coord] = set()
                            vertex_dict[coord].add(kdepum)
                            if coord not in geom_dict:
                                geom_dict[coord] = arcpy.PointGeometry(pnt, work_sr)

        arcpy.AddMessage("3. Mendeteksi Simpul Pertemuan dan Ujung Batas (Pinggir AOI)...")
        tk_coords = set()

        def process_ring(pts):
            n = len(pts)
            if n < 3: return
            
            for i in range(n - 1): 
                coord = pts[i]
                shared_by = vertex_dict.get(coord, set())
                
                if len(shared_by) >= 3:
                    tk_coords.add(coord)
                elif len(shared_by) == 2:
                    prev_idx = (n - 2) if i == 0 else (i - 1)
                    next_idx = (i + 1)
                    
                    prev_coord = pts[prev_idx]
                    next_coord = pts[next_idx]
                    
                    prev_shared = vertex_dict.get(prev_coord, set())
                    next_shared = vertex_dict.get(next_coord, set())
                    
                    if len(prev_shared) < 2 or len(next_shared) < 2:
                        tk_coords.add(coord)

        with arcpy.da.SearchCursor(work_fc, ['SHAPE@', kdepum_field]) as cursor:
            for row in cursor:
                geom = row[0]
                val = row[1]
                if not geom or val is None or str(val).strip() == "": 
                    continue
                for part in geom:
                    pts = []
                    for pnt in part:
                        if pnt:
                            pts.append((round(pnt.X, 2), round(pnt.Y, 2)))
                        else: 
                            process_ring(pts)
                            pts = []
                    if pts:
                        process_ring(pts)

        arcpy.AddMessage("   -> Ditemukan {0} Titik Kartometrik.".format(len(tk_coords)))

        arcpy.AddMessage("4. Menyusun Format Penamaan, Menghitung Koordinat, dan Menyimpan Data...")
        temp_out = os.path.join(scratch, "temp_tk_out")
        if arcpy.Exists(temp_out): arcpy.Delete_management(temp_out)
        
        arcpy.CreateFeatureclass_management(scratch, "temp_tk_out", "POINT", spatial_reference=work_sr)
        
        # Pembuatan Kolom Atribut: Nama_TK, KOORDY_DMS, KOORDX_DMS, UTMX, UTMY
        arcpy.AddField_management(temp_out, "Nama_TK", "TEXT", field_length=255)
        arcpy.AddField_management(temp_out, "KOORDY_DMS", "TEXT", field_length=50)
        arcpy.AddField_management(temp_out, "KOORDX_DMS", "TEXT", field_length=50)
        arcpy.AddField_management(temp_out, "UTMX", "DOUBLE")
        arcpy.AddField_management(temp_out, "UTMY", "DOUBLE")

        with arcpy.da.InsertCursor(temp_out, ['SHAPE@', 'Nama_TK', 'KOORDY_DMS', 'KOORDX_DMS', 'UTMX', 'UTMY']) as ic:
            for coord in tk_coords:
                kdepum_set = vertex_dict.get(coord, set())
                if len(kdepum_set) < 2: continue 
                
                kdepums = sorted(list(kdepum_set))
                
                first = kdepums[0]
                name = "TK " + first
                
                for k in kdepums[1:]:
                    parts = k.split('.')
                    if len(parts) >= 4:
                        name += "-{0}.{1}".format(parts[2], parts[3])
                    else:
                        name += "-" + k
                        
                name += "-000"
                
                pt_geom = geom_dict[coord]
                
                # Menggunakan round(..., 2) untuk memastikan 2 angka di belakang koma
                x_utm = round(pt_geom.firstPoint.X, 2)
                y_utm = round(pt_geom.firstPoint.Y, 2)
                
                pt_gcs = pt_geom.projectAs(base_gcs)
                lon_dd = pt_gcs.firstPoint.X
                lat_dd = pt_gcs.firstPoint.Y
                
                koordy_dms_str = self.dd_to_dms(lat_dd, is_lat=True)
                koordx_dms_str = self.dd_to_dms(lon_dd, is_lat=False)
                
                ic.insertRow([pt_geom, name, koordy_dms_str, koordx_dms_str, x_utm, y_utm])

        arcpy.AddMessage("5. Mengembalikan Proyeksi dan Membersihkan Data...")
        if sr.type == "Geographic":
            arcpy.Project_management(temp_out, out_fc, sr)
        else:
            arcpy.CopyFeatures_management(temp_out, out_fc)

        for fc in [work_fc, temp_out]:
            if arcpy.Exists(fc):
                try: arcpy.Delete_management(fc)
                except: pass

        arcpy.AddMessage("SELESAI! Seluruh TK beserta kolom Nama_TK, KOORDY_DMS, KOORDX_DMS, UTMX, dan UTMY berhasil dibuat.")
        return
        
# =========================================================
# TOOL 2: PENAMAAN TK BIASA
# =========================================================
class AutoNameTKBiasa(object):
    def __init__(self):
        self.label = "2. Auto-Generate Nama TK"
        self.description = "Penamaan TK Biasa dengan deteksi spasial (Garis Pantai, Batas Kabupaten, Batas Kecamatan), KDEPUM, serta generate otomatis kolom Nama_TK, KOORDY_DMS, KOORDX_DMS, UTMX, dan UTMY."
        self.canRunInBackground = False
    
    def getParameterInfo(self):
        param_batas = arcpy.Parameter(
            displayName="Batas Desa (Polygon)",
            name="batas_desa",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input")
        param_batas.filter.list = ["Polygon"]

        param_kdepum = arcpy.Parameter(
            displayName="Field Kode Desa/Unsur (KDEPUM)",
            name="field_kdepum",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param_kdepum.parameterDependencies = [param_batas.name]

        param_kab = arcpy.Parameter(
            displayName="Batas Kabupaten (Polygon / Polyline) - Opsional",
            name="batas_kab",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input")
        param_kab.filter.list = ["Polygon", "Polyline"]

        param_kec = arcpy.Parameter(
            displayName="Batas Kecamatan (Polygon / Polyline) - Opsional",
            name="batas_kec",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input")
        param_kec.filter.list = ["Polygon", "Polyline"]

        param_pantai = arcpy.Parameter(
            displayName="Garis Pantai (Polyline) - Opsional",
            name="garis_pantai",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input")
        param_pantai.filter.list = ["Polyline"]

        param_simpul = arcpy.Parameter(
            displayName="TK Simpul (Point)",
            name="tk_simpul",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input")
        param_simpul.filter.list = ["Point"]

        param_id_simpul = arcpy.Parameter(
            displayName="Field Nama TK Simpul (Misal: Nama_TK)",
            name="field_id_simpul",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param_id_simpul.parameterDependencies = [param_simpul.name]

        param_biasa = arcpy.Parameter(
            displayName="TK Biasa (Point)",
            name="tk_biasa",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input")
        param_biasa.filter.list = ["Point"]

        return [param_batas, param_kdepum, param_kab, param_kec, param_pantai, 
                param_simpul, param_id_simpul, param_biasa]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        return

    def get_base(self, name):
        if name is None: return ""
        val = name.encode('utf-8') if sys.version_info[0] < 3 and isinstance(name, unicode) else str(name)
        val = re.sub(r'^TK\s+', '', val, flags=re.IGNORECASE)
        val = re.sub(r'-000$', '', val)
        return val.strip()

    def parse_simpul_code(self, name):
        """
        Mengekstrak tuple perbandingan dari nama TK Simpul dengan mengutamakan 
        kode kecamatan terlebih dahulu, baru kemudian kode desa.
        Contoh: 'TK 32.01.14.2005-15.2001-000' -> diekstrak menjadi list tuple:
        [(14, 2005, 32, 1), (15, 2001, 32, 1)]
        """
        base = self.get_base(name)
        if not base:
            return [(0, 0, 0, 0)]
            
        tokens = [t.strip() for t in base.split('-') if t.strip()]
        if not tokens:
            return [(0, 0, 0, 0)]

        def to_int_or_str(s):
            return int(s) if s.isdigit() else s

        first_parts = tokens[0].split('.')
        prefix_prov = to_int_or_str(first_parts[0]) if len(first_parts) >= 1 else 0
        prefix_kab  = to_int_or_str(first_parts[1]) if len(first_parts) >= 2 else 0
        prefix_kec  = to_int_or_str(first_parts[2]) if len(first_parts) >= 3 else 0

        parsed_codes = []
        for tok in tokens:
            pts = [to_int_or_str(p) for p in tok.split('.')]
            if len(pts) >= 4:
                # Format lengkap: Prov.Kab.Kec.Desa -> diurutkan (Kec, Desa, Prov, Kab)
                # agar perbandingan selalu mengutamakan Kode Kecamatan dulu, baru Kode Desa
                prov, kab, kec, desa = pts[0], pts[1], pts[2], pts[3]
                parsed_codes.append((kec, desa, prov, kab))
            elif len(pts) == 2:
                # Format singkat: Kec.Desa (contoh: 15.2001)
                kec, desa = pts[0], pts[1]
                parsed_codes.append((kec, desa, prefix_prov, prefix_kab))
            elif len(pts) == 1:
                # Format hanya kode desa atau nomor tunggal
                desa = pts[0]
                parsed_codes.append((prefix_kec, desa, prefix_prov, prefix_kab))
            else:
                parsed_codes.append(tuple(pts))

        return parsed_codes

    def poly_to_polyline_safe(self, poly_geom, work_sr):
        """
        Konversi Polygon menjadi Polyline secara aman.
        Jika .boundary() gagal karena geometri corrupt di ArcObjects (ValueError),
        otomatis beralih ke rekonstruksi titik manual dari array part poligon.
        """
        if not poly_geom:
            return None
        try:
            b = poly_geom.boundary()
            if b and b.length > 0:
                return b
        except Exception:
            pass

        # Fallback: Ekstrak ring dari part poligon secara manual jika .boundary() crash
        try:
            parts = arcpy.Array()
            for part in poly_geom:
                ring = arcpy.Array()
                for pt in part:
                    if pt:
                        ring.add(pt)
                    else:
                        # Pemisah interior ring (hole/pulau di dalam poligon)
                        if ring.count > 1:
                            parts.add(ring)
                        ring = arcpy.Array()
                if ring.count > 1:
                    parts.add(ring)
            if parts.count > 0:
                line = arcpy.Polyline(parts, work_sr)
                if line and line.length > 0:
                    return line
        except Exception:
            pass
        return None

    def load_reference_lines(self, fc, work_sr):
        """
        Membaca layer referensi (Polygon/Polyline) dan mengubahnya menjadi list Polyline
        agar perhitungan jarak (distanceTo) akurat terhadap garis batasnya.
        Dilengkapi proteksi anti-crash terhadap geometri rusak.
        """
        geoms = []
        if not fc:
            return geoms
        desc = arcpy.Describe(fc)
        is_poly = (desc.shapeType.lower() == "polygon")
        with arcpy.da.SearchCursor(fc, ["SHAPE@"], spatial_reference=work_sr) as cur:
            for row in cur:
                g = row[0]
                if not g:
                    continue
                if is_poly:
                    b = self.poly_to_polyline_safe(g, work_sr)
                    if b:
                        geoms.append(b)
                else:
                    try:
                        if g.length > 0:
                            geoms.append(g)
                    except Exception:
                        pass
        return geoms

    def dd_to_dms(self, dd, is_lat=True):
        """Konversi Decimal Degrees ke format DMS dengan 3 angka di belakang koma"""
        if dd is None:
            return u"-"
        direction = u"N" if (is_lat and dd >= 0) else (u"S" if is_lat else (u"E" if dd >= 0 else u"W"))
            
        dd_abs = abs(float(dd))
        degrees = int(dd_abs)
        minutes = int((dd_abs - degrees) * 60.0)
        seconds = (((dd_abs - degrees) * 60.0) - minutes) * 60.0
        
        if round(seconds, 3) >= 60.0:
            seconds = 0.0
            minutes += 1
        if minutes >= 60:
            minutes = 0
            degrees += 1
            
        sec_str = u"{0:06.3f}".format(seconds).replace(u".", u",")
        return u"{0}\u00B0 {1}' {2}\" {3}".format(degrees, minutes, sec_str, direction)

    def ensure_fields(self, fc, include_nama_tk=True):
        """Membuat kolom Nama_TK, KOORDY_DMS, KOORDX_DMS, UTMX, UTMY secara otomatis jika belum ada"""
        existing = [f.name.upper() for f in arcpy.ListFields(fc)]
        
        req_fields = []
        if include_nama_tk:
            req_fields.append(("Nama_TK", "TEXT", 255))
            
        req_fields.extend([
            ("KOORDY_DMS", "TEXT", 50),
            ("KOORDX_DMS", "TEXT", 50),
            ("UTMX", "DOUBLE", None),
            ("UTMY", "DOUBLE", None)
        ])
        
        for fname, ftype, flen in req_fields:
            if fname.upper() not in existing:
                if flen:
                    arcpy.AddField_management(fc, fname, ftype, field_length=flen)
                else:
                    arcpy.AddField_management(fc, fname, ftype)

        field_map = {}
        targets = ["NAMA_TK", "KOORDY_DMS", "KOORDX_DMS", "UTMX", "UTMY"]
        for f in arcpy.ListFields(fc):
            for t in targets:
                if f.name.upper() == t:
                    field_map[t] = f.name
        return field_map

    def execute(self, parameters, messages):
        batas_desa   = parameters[0].valueAsText
        f_kdepum     = parameters[1].valueAsText
        batas_kab    = parameters[2].valueAsText
        batas_kec    = parameters[3].valueAsText
        garis_pantai = parameters[4].valueAsText
        tk_simpul    = parameters[5].valueAsText
        f_id_simpul  = parameters[6].valueAsText
        tk_biasa     = parameters[7].valueAsText

        arcpy.env.overwriteOutput = True

        arcpy.AddMessage("1. Mempersiapkan Proyeksi (Auto UTM)...")
        desc = arcpy.Describe(batas_desa)
        sr = desc.spatialReference
        base_gcs = sr if sr.type == "Geographic" else sr.GCS
        if not base_gcs:
            base_gcs = arcpy.SpatialReference(4326)
        
        ext_gcs = desc.extent.projectAs(base_gcs) if sr.type != "Geographic" else desc.extent
        cen_x = (ext_gcs.XMin + ext_gcs.XMax) / 2.0
        cen_y = (ext_gcs.YMin + ext_gcs.YMax) / 2.0
        utm_zone = int((cen_x + 180) / 6) + 1
        hemisphere = "S" if cen_y < 0 else "N"
        zona_utm_str = "{0}{1}".format(utm_zone, hemisphere)
        epsg = (32700 + utm_zone) if cen_y < 0 else (32600 + utm_zone)
        utm_sr = arcpy.SpatialReference(epsg)

        work_sr = utm_sr if sr.type == "Geographic" else sr
        arcpy.AddMessage("   -> Zona UTM Terdeteksi: {0} (EPSG:{1})".format(zona_utm_str, epsg))

        arcpy.AddMessage("2. Membaca geometri spasial (Desa, Pantai, Kabupaten, Kecamatan)...")
        pantai_geoms = self.load_reference_lines(garis_pantai, work_sr)
        kab_geoms    = self.load_reference_lines(batas_kab, work_sr)
        kec_geoms    = self.load_reference_lines(batas_kec, work_sr)

        polys = []
        with arcpy.da.SearchCursor(batas_desa, ["OID@", f_kdepum, "SHAPE@"], spatial_reference=work_sr) as cur:
            for row in cur:
                if row[2]:
                    polys.append({
                        'oid': row[0],
                        'kdepum': str(row[1]).strip() if row[1] else "",
                        'geom': row[2]
                    })

        # Toleransi deteksi sentuhan titik simpul ke garis referensi (meter)
        TOL_REF = 15.0  

        arcpy.AddMessage("3. Memetakan Simpul dan Menghitung Jarak ke Garis Referensi...")
        simpul_pts = []
        
        with arcpy.da.SearchCursor(tk_simpul, ["OID@", f_id_simpul, "SHAPE@"], spatial_reference=work_sr) as cur:
            for row in cur:
                oid, name, geom = row[0], row[1], row[2]
                if not geom: continue
                
                dist_pantai = min([pg.distanceTo(geom) for pg in pantai_geoms]) if pantai_geoms else float('inf')
                dist_kab    = min([kg.distanceTo(geom) for kg in kab_geoms])    if kab_geoms    else float('inf')
                dist_kec    = min([cg.distanceTo(geom) for cg in kec_geoms])    if kec_geoms    else float('inf')

                simpul_pts.append({
                    'oid': oid, 
                    'name': name, 
                    'geom': geom, 
                    'dist_pantai': dist_pantai,
                    'dist_kab': dist_kab,
                    'dist_kec': dist_kec,
                    'on_pantai': (dist_pantai <= TOL_REF),
                    'on_kab': (dist_kab <= TOL_REF),
                    'on_kec': (dist_kec <= TOL_REF)
                })

        biasa_pts = []
        with arcpy.da.SearchCursor(tk_biasa, ["OID@", "SHAPE@"], spatial_reference=work_sr) as cur:
            for row in cur:
                if row[1]: biasa_pts.append({'oid': row[0], 'geom': row[1]})

        arcpy.AddMessage("4. Evaluasi Hierarki Batas & Pemberian Nama Urutan TK Biasa...")
        hasil_nama = {}
        arcpy.SetProgressor("step", "Memproses Segmen...", 0, len(polys), 1)
        
        for poly in polys:
            boundary = self.poly_to_polyline_safe(poly['geom'], work_sr)
            if not boundary:
                arcpy.AddWarning("   -> Peringatan: Geometri poligon desa OID {} rusak/kosong dan dilewati.".format(poly['oid']))
                arcpy.SetProgressorPosition()
                continue
                
            line_len = boundary.length
            pts_on_bound = []
            
            for s in simpul_pts:
                try:
                    if boundary.distanceTo(s['geom']) <= 1.0:
                        pts_on_bound.append({
                            'type': 'S', 
                            'oid': s['oid'], 
                            'name': s['name'],
                            'measure': boundary.measureOnLine(s['geom']), 
                            'geom': s['geom'],
                            'dist_pantai': s['dist_pantai'],
                            'dist_kab': s['dist_kab'],
                            'dist_kec': s['dist_kec'],
                            'on_pantai': s['on_pantai'],
                            'on_kab': s['on_kab'],
                            'on_kec': s['on_kec']
                        })
                except Exception:
                    pass
                    
            for b in biasa_pts:
                try:
                    if boundary.distanceTo(b['geom']) <= 1.0:
                        pts_on_bound.append({
                            'type': 'B', 
                            'oid': b['oid'],
                            'measure': boundary.measureOnLine(b['geom']), 
                            'geom': b['geom']
                        })
                except Exception:
                    pass

            if len([p for p in pts_on_bound if p['type'] == 'S']) < 2:
                arcpy.SetProgressorPosition()
                continue
                
            pts_on_bound.sort(key=lambda x: x['measure'])

            first_s_idx = next(i for i, pt in enumerate(pts_on_bound) if pt['type'] == 'S')
            pts_on_bound = pts_on_bound[first_s_idx:] + pts_on_bound[:first_s_idx]

            last_simpul = pts_on_bound[0].copy()
            last_simpul['measure'] += line_len
            pts_on_bound.append(last_simpul)

            segments = []
            current_seg = []
            for pt in pts_on_bound:
                current_seg.append(pt)
                if pt['type'] == 'S' and len(current_seg) > 1:
                    segments.append(current_seg)
                    current_seg = [pt]

            for seg in segments:
                s_A, s_B = seg[0], seg[-1]
                biasas = [pt for pt in seg[1:-1] if pt['type'] == 'B']
                if not biasas: continue

                # --- A. PENENTUAN PREFIX KDEPUM ---
                test_pt = biasas[0]['geom']
                intersecting_desa = []
                for p in polys:
                    try:
                        if p['geom'].distanceTo(test_pt) <= 0.5:
                            intersecting_desa.append(p)
                    except Exception:
                        pass
                        
                if len(intersecting_desa) < 2:
                    intersecting_desa = []
                    for p in polys:
                        try:
                            if p['geom'].distanceTo(test_pt) <= 2.0:
                                intersecting_desa.append(p)
                        except Exception:
                            pass
                
                valid_kdepums = sorted(list(set([p['kdepum'] for p in intersecting_desa if p['kdepum']])))
                
                if len(valid_kdepums) == 0:
                    prefix = "TK UNKNOWN"
                elif len(valid_kdepums) == 1:
                    prefix = "TK {0}".format(valid_kdepums[0])
                else:
                    k1 = valid_kdepums[0]
                    k2 = valid_kdepums[1] 
                    parts2 = k2.split('.')
                    if len(parts2) >= 4:
                        k2_short = "{0}.{1}".format(parts2[-2], parts2[-1])
                    else:
                        k2_short = k2
                    prefix = "TK {0}-{1}".format(k1, k2_short)

                # --- B. PENENTUAN ARAH URUTAN (ORIGIN -> DESTINATION) ---
                # Aturan: Simpul yang menyentuh hirarki lebih tinggi menjadi TITIK AKHIR (dest)
                origin, dest = None, None

                # Prioritas 1: Garis Pantai
                if s_A['on_pantai'] != s_B['on_pantai']:
                    if s_A['on_pantai']:
                        dest, origin = s_A, s_B
                    else:
                        dest, origin = s_B, s_A
                elif s_A['on_pantai'] and s_B['on_pantai']:
                    # Jika keduanya di dekat pantai, yang lebih dekat ke garis pantai menjadi titik akhir
                    if abs(s_A['dist_pantai'] - s_B['dist_pantai']) > 5.0:
                        if s_A['dist_pantai'] < s_B['dist_pantai']:
                            dest, origin = s_A, s_B
                        else:
                            dest, origin = s_B, s_A

                # Prioritas 2: Batas Kabupaten (Jika belum ditentukan oleh pantai)
                if not dest:
                    if s_A['on_kab'] != s_B['on_kab']:
                        if s_A['on_kab']:
                            dest, origin = s_A, s_B
                        else:
                            dest, origin = s_B, s_A

                # Prioritas 3: Batas Kecamatan (Jika belum ditentukan oleh pantai/kabupaten)
                if not dest:
                    if s_A['on_kec'] != s_B['on_kec']:
                        if s_A['on_kec']:
                            dest, origin = s_A, s_B
                        else:
                            dest, origin = s_B, s_A

                # Prioritas 4 (Fallback): Kode TK Simpul (Utamakan Kode Kecamatan, baru Kode Desa)
                # Simpul dengan kode LEBIH BESAR menjadi urutan terakhir (dest)
                if not dest:
                    code_A = self.parse_simpul_code(s_A['name'])
                    code_B = self.parse_simpul_code(s_B['name'])
                    if code_A < code_B:
                        origin, dest = s_A, s_B
                    else:
                        origin, dest = s_B, s_A

                # Jika titik awal (origin) adalah s_B, balik urutan titik biasa pada segmen ini
                if origin['oid'] == s_B['oid'] and origin['measure'] == s_B['measure']:
                    biasas.reverse()

                for i, b in enumerate(biasas):
                    num = i + 1
                    name_str = "{0}-{1:03d}".format(prefix, num)
                    hasil_nama[b['oid']] = name_str

            arcpy.SetProgressorPosition()

        arcpy.AddMessage("5. Menyiapkan Kolom & Menuliskan Nama_TK serta Koordinat ke Tabel...")
        
        f_map_biasa = self.ensure_fields(tk_biasa, include_nama_tk=True)
        cursor_fields_biasa = [
            "OID@", "SHAPE@", 
            f_map_biasa["NAMA_TK"],
            f_map_biasa["KOORDY_DMS"],
            f_map_biasa["KOORDX_DMS"],
            f_map_biasa["UTMX"],
            f_map_biasa["UTMY"]
        ]

        with arcpy.da.UpdateCursor(tk_biasa, cursor_fields_biasa) as cur:
            for row in cur:
                oid = row[0]
                geom = row[1]
                
                if oid in hasil_nama:
                    row[2] = hasil_nama[oid]
                
                if geom:
                    pt_utm = geom.projectAs(work_sr).firstPoint
                    pt_gcs = geom.projectAs(base_gcs).firstPoint
                    
                    row[3] = self.dd_to_dms(pt_gcs.Y, is_lat=True)
                    row[4] = self.dd_to_dms(pt_gcs.X, is_lat=False)
                    row[5] = round(pt_utm.X, 2)
                    row[6] = round(pt_utm.Y, 2)
                    
                cur.updateRow(row)

        desc_biasa = arcpy.Describe(tk_biasa)
        desc_simpul = arcpy.Describe(tk_simpul)
        if desc_biasa.catalogPath != desc_simpul.catalogPath:
            arcpy.AddMessage("   -> Melengkapi kolom koordinat pada layer TK Simpul...")
            f_map_simpul = self.ensure_fields(tk_simpul, include_nama_tk=False)
            cursor_fields_simpul = [
                "SHAPE@",
                f_map_simpul["KOORDY_DMS"],
                f_map_simpul["KOORDX_DMS"],
                f_map_simpul["UTMX"],
                f_map_simpul["UTMY"]
            ]
            with arcpy.da.UpdateCursor(tk_simpul, cursor_fields_simpul) as cur_s:
                for row_s in cur_s:
                    geom_s = row_s[0]
                    if geom_s:
                        pt_utm = geom_s.projectAs(work_sr).firstPoint
                        pt_gcs = geom_s.projectAs(base_gcs).firstPoint
                        row_s[1] = self.dd_to_dms(pt_gcs.Y, is_lat=True)
                        row_s[2] = self.dd_to_dms(pt_gcs.X, is_lat=False)
                        row_s[3] = round(pt_utm.X, 2)
                        row_s[4] = round(pt_utm.Y, 2)
                        cur_s.updateRow(row_s)

        arcpy.AddMessage("SELESAI! Kolom Nama_TK, KOORDY_DMS, KOORDX_DMS, UTMX, dan UTMY berhasil dibuat dan diisi.")
        return

# =========================================================
# TOOL 3: QUALITY CONTROL (QC)
# =========================================================
class QCTitikKartometrik(object):
    def __init__(self):
        self.label = "3. QC Topologi & Atribut TK"
        self.description = "Quality Control jarak Snap (5cm) dan KDEPUM secara spasial."
        self.canRunInBackground = False
    
    def getParameterInfo(self):
        param_batas = arcpy.Parameter(
            displayName="Batas Administrasi / Segmen (Polygon)",
            name="batas_admin",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input")
        param_batas.filter.list = ["Polygon"]

        param_kdepum = arcpy.Parameter(
            displayName="Field Kode Desa (KDEPUM)",
            name="field_kdepum",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param_kdepum.parameterDependencies = [param_batas.name]

        param_tk1 = arcpy.Parameter(
            displayName="Titik Kartometrik 1 (Wajib)",
            name="tk_1",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input")
        param_tk1.filter.list = ["Point"]

        param_f_tk1 = arcpy.Parameter(
            displayName="Field Nama TK 1",
            name="field_nama1",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param_f_tk1.parameterDependencies = [param_tk1.name]

        param_tk2 = arcpy.Parameter(
            displayName="Titik Kartometrik 2 (Opsional)",
            name="tk_2",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input")
        param_tk2.filter.list = ["Point"]

        param_f_tk2 = arcpy.Parameter(
            displayName="Field Nama TK 2",
            name="field_nama2",
            datatype="Field",
            parameterType="Optional",
            direction="Input")
        param_f_tk2.parameterDependencies = [param_tk2.name]

        return [param_batas, param_kdepum, param_tk1, param_f_tk1, param_tk2, param_f_tk2]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        if parameters[4].value:
            parameters[5].enabled = True
        else:
            parameters[5].enabled = False
        return

    def updateMessages(self, parameters):
        return

    def execute(self, parameters, messages):
        batas_fc = parameters[0].valueAsText
        f_kdepum = parameters[1].valueAsText
        tk1_fc = parameters[2].valueAsText
        f_tk1 = parameters[3].valueAsText
        tk2_fc = parameters[4].valueAsText
        f_tk2 = parameters[5].valueAsText if tk2_fc else None

        arcpy.env.overwriteOutput = True

        desc_batas = arcpy.Describe(batas_fc)
        sr_batas = desc_batas.spatialReference
        
        if sr_batas.type == "Geographic":
            ext = desc_batas.extent
            cen_x = (ext.XMin + ext.XMax) / 2.0
            utm_zone = int((cen_x + 180) / 6) + 1
            epsg = 32700 + utm_zone if ext.YMin < 0 else 32600 + utm_zone
            work_sr = arcpy.SpatialReference(epsg)
            arcpy.AddMessage("Auto UTM Zone {0} diaktifkan.".format(utm_zone))
        else:
            work_sr = sr_batas

        arcpy.AddMessage("2. Membaca geometri Poligon & KDEPUM ke dalam memori...")
        polys = []
        parts_array = arcpy.Array()
        
        with arcpy.da.SearchCursor(batas_fc, ["SHAPE@", f_kdepum], spatial_reference=work_sr) as cur:
            for row in cur:
                geom = row[0]
                val_kdepum = str(row[1]).strip() if row[1] else ""
                if not geom: continue
                
                polys.append({
                    'geom': geom,
                    'kdepum': val_kdepum
                })
                
                boundary = geom.boundary()
                for part in boundary:
                    parts_array.add(part)
                    
        master_line = arcpy.Polyline(parts_array, work_sr)

        def process_tk(fc, name_field, label):
            arcpy.AddMessage("3. Menjalankan QC Spasial pada {0}...".format(label))
            
            desc_fc = arcpy.Describe(fc)
            workspace = desc_fc.path
            
            desc_ws = arcpy.Describe(workspace)
            if desc_ws.dataType == "FeatureDataset":
                workspace = desc_ws.path

            fields = [f.name for f in arcpy.ListFields(fc)]
            if "QC_Status" not in fields:
                arcpy.AddField_management(fc, "QC_Status", "TEXT", field_length=255)

            TOLERANSI_SNAP = 0.05 # Toleransi Snap 5 cm
            error_count, valid_count = 0, 0

            edit = arcpy.da.Editor(workspace)
            edit.startEditing(False, True) 
            edit.startOperation()

            try:
                with arcpy.da.UpdateCursor(fc, ["SHAPE@", name_field, "QC_Status"], spatial_reference=work_sr) as cur:
                    for row in cur:
                        geom = row[0]
                        raw_name = row[1]
                        if raw_name is None: raw_name = ""
                        name_val = raw_name.encode('utf-8') if sys.version_info[0] < 3 and isinstance(raw_name, unicode) else str(raw_name)
                        name_val = name_val.strip()
                        
                        status_list = []
                        
                        # 1. CEK GEOMETRI/KOSONG
                        if not geom or name_val == "":
                            status_list.append("GEOMETRI/NAMA KOSONG")
                        else:
                            # 2. CEK SNAP TOPOLOGI (5cm)
                            dist = geom.distanceTo(master_line)
                            if dist > TOLERANSI_SNAP:
                                status_list.append("TDK SNAP ({0:.2f}m)".format(dist))

                            # 3. KUMPULKAN POLIGON TERDEKAT (Radius diperbesar jadi 2.0 meter)
                            intersecting = [p for p in polys if p['geom'].distanceTo(geom) <= 2.0]
                            valid_kdepums = list(set([p['kdepum'] for p in intersecting if p['kdepum'] != ""]))

                            # 4. SMART PARSING ATRIBUT NAMA TK
                            match = re.match(r'^TK\s+([\d\.]+)(.*)-(\d{3})$', name_val)
                            if not match:
                                status_list.append("FORMAT SALAH (Pastikan penamaan mengikuti standar TK XXXX-XXX)")
                            else:
                                base_kdepum = match.group(1)
                                middle_part = match.group(2)
                                suffix = match.group(3)
                                
                                extracted_kdepums = [base_kdepum]
                                
                                # Ekstrak KDEPUM ke-2, ke-3 dst yang dipisah tanda '-'
                                if middle_part:
                                    parts = [p for p in middle_part.split('-') if p]
                                    base_parts = base_kdepum.split('.')
                                    
                                    prefix2 = base_parts[0] + "." + base_parts[1] + "." if len(base_parts) >= 2 else ""
                                    prefix3 = base_parts[0] + "." + base_parts[1] + "." + base_parts[2] + "." if len(base_parts) >= 3 else ""
                                    
                                    for p in parts:
                                        p_split = p.split('.')
                                        if len(p_split) >= 4:
                                            extracted_kdepums.append(p)
                                        elif len(p_split) == 2:
                                            extracted_kdepums.append(prefix2 + p)
                                        elif len(p_split) == 1:
                                            extracted_kdepums.append(prefix3 + p)
                                        else:
                                            extracted_kdepums.append(p)
                                
                                # A. Pastikan semua desa yang terdeteksi secara spasial, ADA tertulis di dalam nama TK
                                missing_in_name = [k for k in valid_kdepums if k not in extracted_kdepums]
                                if missing_in_name:
                                    status_list.append("KDEPUM SALAH (Poligon {0} terdeteksi di peta tapi tidak ada di penamaan)".format(", ".join(missing_in_name)))
                                
                                # B. Cek Akhiran Simpul
                                if len(extracted_kdepums) >= 3 and suffix != "000":
                                    status_list.append("SUFFIX SALAH (Titik pertemuan 3 desa atau lebih harus berakhiran -000)")
                                    
                                # C. Desa luar yang tidak ada poligonnya (Sekedar informasi, bukan error)
                                extra_in_name = [k for k in extracted_kdepums if k not in valid_kdepums]

                        # PENULISAN HASIL
                        if len(status_list) > 0:
                            row[2] = "[ERROR] " + " | ".join(status_list)
                            error_count += 1
                        else:
                            info_msg = " (Info: Poligon luar {0} tdk dlm input batas)".format(", ".join(extra_in_name)) if extra_in_name else ""
                            row[2] = "VALID" + info_msg
                            valid_count += 1
                            
                        cur.updateRow(row)
                
                edit.stopOperation()
                edit.stopEditing(True)

            except Exception as e:
                edit.abortOperation()
                edit.stopEditing(False)
                arcpy.AddError("Gagal memproses Edit Session: " + str(e))
                raise e
                    
            arcpy.AddMessage("   -> Selesai: {0} Valid, {1} Error ditemukan.".format(valid_count, error_count))

        process_tk(tk1_fc, f_tk1, "TK 1")
        if tk2_fc and f_tk2:
            process_tk(tk2_fc, f_tk2, "TK 2")

        arcpy.AddMessage("QC Selesai! Cek kolom 'QC_Status' di tabel atribut layer Anda.")
        return

# =========================================================
# TOOL 4: URUTKAN & EKSPOR KOORDINAT
# =========================================================
class ExportTKExcel(object):
    def __init__(self):
        self.label = "4. Export Atribut TK CCW"
        self.description = "Mengurutkan TK berdasarkan garis batas desa ke Excel."
        self.canRunInBackground = False
    
    def getParameterInfo(self):
        param_batas = arcpy.Parameter(
            displayName="Batas Desa (Polygon)",
            name="batas_desa",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input")
        param_batas.filter.list = ["Polygon"]

        param_wadkmd = arcpy.Parameter(
            displayName="Field Nama Desa (WADKMD/NAMOBJ)",
            name="wadkmd_field",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param_wadkmd.parameterDependencies = [param_batas.name]

        param_tk = arcpy.Parameter(
            displayName="Titik Kartometrik (Point)",
            name="titik_kartometrik",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input")
        param_tk.filter.list = ["Point"]

        param_remark = arcpy.Parameter(
            displayName="Field Nama TK (Remark)",
            name="remark_field",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param_remark.parameterDependencies = [param_tk.name]

        param_lat = arcpy.Parameter(
            displayName="Field Latitude (Lintang)",
            name="lat_field",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param_lat.parameterDependencies = [param_tk.name]

        param_long = arcpy.Parameter(
            displayName="Field Longitude (Bujur)",
            name="long_field",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param_long.parameterDependencies = [param_tk.name]

        param_x = arcpy.Parameter(
            displayName="Field X (UTM)",
            name="x_field",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param_x.parameterDependencies = [param_tk.name]

        param_y = arcpy.Parameter(
            displayName="Field Y (UTM)",
            name="y_field",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param_y.parameterDependencies = [param_tk.name]

        param_out_excel = arcpy.Parameter(
            displayName="Output File Excel",
            name="output_excel",
            datatype="DEFile",
            parameterType="Required",
            direction="Output")
        param_out_excel.filter.list = ["xls", "xlsx"]

        params = [param_batas, param_wadkmd, param_tk, param_remark, 
                  param_lat, param_long, param_x, param_y, param_out_excel]
        return params

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        return

    def is_clockwise(self, polyline):
        area = 0
        part = polyline.getPart(0)
        for i in range(len(part) - 1):
            p1 = part.getObject(i)
            p2 = part.getObject(i + 1)
            if p1 and p2:
                area += (p2.X - p1.X) * (p2.Y + p1.Y)
        return area > 0

    def safe_str(self, val):
        if val is None:
            return ""
        if sys.version_info[0] < 3: 
            if isinstance(val, unicode):
                return val.encode('utf-8').strip()
        return str(val).strip()

    def format_dms(self, val):
        val_str = self.safe_str(val)
        
        deg_sym = u'\u00B0' if sys.version_info[0] >= 3 else '\xc2\xb0'
        
        if deg_sym in val_str:
            parts = val_str.split(deg_sym)
            deg = parts[0].lstrip('0')
            if deg == '':
                deg = '0'
            val_str = deg + deg_sym + deg_sym.join(parts[1:])
        
        def repl(match):
            sec_val = float(match.group(1))
            return "{:.3f}".format(sec_val)
            
        val_str = re.sub(r'(\d+(?:\.\d+)?)(?=\")', repl, val_str)
        val_str = re.sub(r'(\d+(?:\.\d+)?)(?=\'\')', repl, val_str)
        
        return val_str

    def format_utm(self, val):
        try:
            if val is None or str(val).strip() == '':
                return ""
            return "{:.2f}".format(float(val))
        except ValueError:
            return self.safe_str(val)

    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True

        batas_desa = parameters[0].valueAsText
        wadkmd_field = parameters[1].valueAsText
        titik_kartometrik = parameters[2].valueAsText
        remark_field = parameters[3].valueAsText
        lat_field = parameters[4].valueAsText
        long_field = parameters[5].valueAsText
        x_field = parameters[6].valueAsText
        y_field = parameters[7].valueAsText
        output_excel = parameters[8].valueAsText

        arcpy.MakeFeatureLayer_management(titik_kartometrik, "tk_lyr")
        arcpy.MakeFeatureLayer_management(batas_desa, "desa_lyr")

        results = []

        desa_oids = [row[0] for row in arcpy.da.SearchCursor("desa_lyr", ["OID@"])]
        arcpy.SetProgressor("step", "Memproses Batas Desa...", 0, len(desa_oids), 1)

        for oid in desa_oids:
            oid_field = arcpy.Describe("desa_lyr").OIDFieldName
            delimited_field = arcpy.AddFieldDelimiters("desa_lyr", oid_field)
            where_clause = "{} = {}".format(delimited_field, oid)
            
            arcpy.SelectLayerByAttribute_management("desa_lyr", "NEW_SELECTION", where_clause)

            with arcpy.da.SearchCursor("desa_lyr", [wadkmd_field, "SHAPE@"]) as cur:
                try:
                    row = next(cur)
                    desa_name = self.safe_str(row[0])
                    polygon = row[1]
                except StopIteration:
                    continue

            if not polygon:
                arcpy.SetProgressorPosition()
                continue

            boundary_line = polygon.boundary()
            batas_cw = self.is_clockwise(boundary_line)

            arcpy.SelectLayerByLocation_management("tk_lyr", "INTERSECT", boundary_line, "1 Meters", "NEW_SELECTION")

            intersecting_tk = []
            fields_tk = ["SHAPE@", remark_field, "SHAPE@X", lat_field, long_field, x_field, y_field]
            
            with arcpy.da.SearchCursor("tk_lyr", fields_tk) as tk_cur:
                for tk_row in tk_cur:
                    geom = tk_row[0]
                    remark = tk_row[1]
                    x_coord = tk_row[2]
                    
                    val_lat = self.format_dms(tk_row[3])
                    val_long = self.format_dms(tk_row[4])
                    val_x = self.format_utm(tk_row[5])
                    val_y = self.format_utm(tk_row[6])
                    
                    pos = boundary_line.measureOnLine(geom)
                    intersecting_tk.append({
                        "remark": self.safe_str(remark),
                        "x": x_coord,
                        "pos_on_line": pos,
                        "lat": val_lat,
                        "long": val_long,
                        "x_val": val_x,
                        "y_val": val_y
                    })

            if intersecting_tk:
                intersecting_tk.sort(key=lambda item: item["pos_on_line"])

                if batas_cw:
                    intersecting_tk.reverse()

                candidates_000 = [i for i, tk in enumerate(intersecting_tk) if tk["remark"].endswith("-000")]
                
                if candidates_000:
                    start_index = min(candidates_000, key=lambda i: intersecting_tk[i]["x"])
                else:
                    start_index = min(range(len(intersecting_tk)), key=lambda i: intersecting_tk[i]["x"])

                ordered_tk = intersecting_tk[start_index:] + intersecting_tk[:start_index]
                ordered_tk.append(ordered_tk[0])

                no_urut = 1
                for tk in ordered_tk:
                    results.append([
                        no_urut,
                        desa_name, 
                        tk["remark"], 
                        tk["lat"], 
                        tk["long"], 
                        tk["x_val"], 
                        tk["y_val"]
                    ])
                    no_urut += 1
                
            arcpy.SetProgressorPosition()

        arcpy.SetProgressor("step", "Menulis ke file Excel...", 0, 1, 1)

        if not output_excel.lower().endswith(('.xls', '.xlsx')):
            output_excel += '.xls'

        temp_tbl = "in_memory\\Hasil_TK"
        if arcpy.Exists(temp_tbl):
            arcpy.Delete_management(temp_tbl)

        arcpy.CreateTable_management("in_memory", "Hasil_TK")

        arcpy.AddField_management(temp_tbl, "No", "LONG")
        arcpy.AddField_management(temp_tbl, "Desa", "TEXT", field_length=255)
        arcpy.AddField_management(temp_tbl, "Urutan_TK", "TEXT", field_length=255)
        arcpy.AddField_management(temp_tbl, "Lat", "TEXT", field_alias=lat_field, field_length=255)
        arcpy.AddField_management(temp_tbl, "Long", "TEXT", field_alias=long_field, field_length=255)
        arcpy.AddField_management(temp_tbl, "UTMX", "TEXT", field_alias=x_field, field_length=255)
        arcpy.AddField_management(temp_tbl, "UTMY", "TEXT", field_alias=y_field, field_length=255)

        with arcpy.da.InsertCursor(temp_tbl, ["No", "Desa", "Urutan_TK", "Lat", "Long", "UTMX", "UTMY"]) as icur:
            for row in results:
                icur.insertRow(row)

        arcpy.TableToExcel_conversion(temp_tbl, output_excel, True)
        arcpy.Delete_management(temp_tbl)

        arcpy.AddMessage("Eksekusi selesai. File Excel disimpan di: {}".format(output_excel))
        return

# =========================================================
# TOOL 5: LAPORAN DESKRIPSI (INCLUDE KAB, PANTAI, JALAN/SUNGAI PINTAR)
# =========================================================
class ExportLaporanTK(object):
    def __init__(self):
        self.label = "5. Generate Deskripsi TK (Include XY, LatLong)"
        self.description = "Ekstrak deskripsi lokasi TK ke Excel. Mendukung redaksi cerdas (jalan/sungai), deteksi Batas Kabupaten tetangga dinamis (pengecualian host), dan referensi hukum."
        self.canRunInBackground = False
        
    def getParameterInfo(self):
        param_simpul = arcpy.Parameter(displayName="Titik Kartometrik 1 (Simpul)", name="in_tk_simpul", datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        param_simpul.filter.list = ["Point"]

        param_biasa = arcpy.Parameter(displayName="Titik Kartometrik 2 (TK Biasa - Opsional)", name="in_tk_biasa", datatype="GPFeatureLayer", parameterType="Optional", direction="Input")
        param_biasa.filter.list = ["Point"]

        param_segmen = arcpy.Parameter(displayName="Batas Segmen (Polyline)", name="in_segmen", datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        param_segmen.filter.list = ["Polyline"]
        
        param_f_segmen = arcpy.Parameter(displayName="Pilih Kolom Nama Segmen (Contoh: NAMOBJ)", name="field_segmen", datatype="Field", parameterType="Required", direction="Input")
        param_f_segmen.parameterDependencies = [param_segmen.name]

        param_desa = arcpy.Parameter(displayName="Batas Desa (Polygon)", name="in_desa", datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        param_desa.filter.list = ["Polygon"]

        param_pl = arcpy.Parameter(displayName="Penggunaan Lahan / PL (Polygon)", name="in_pl", datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        param_pl.filter.list = ["Polygon"]
        
        param_f_pl = arcpy.Parameter(displayName="Pilih Kolom PL (Pilih REMARK / NAMOBJ, JANGAN FCODE)", name="field_pl", datatype="Field", parameterType="Required", direction="Input")
        param_f_pl.parameterDependencies = [param_pl.name]

        param_jalan = arcpy.Parameter(displayName="Jaringan Jalan (Polyline)", name="in_jalan", datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        param_jalan.filter.list = ["Polyline"]

        param_f_jalan = arcpy.Parameter(displayName="Pilih Kolom Nama Jalan", name="field_jalan", datatype="Field", parameterType="Required", direction="Input")
        param_f_jalan.parameterDependencies = [param_jalan.name]

        param_sungai = arcpy.Parameter(displayName="Jaringan Perairan (Polyline/Polygon)", name="in_sungai", datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        
        param_f_jns_sungai = arcpy.Parameter(displayName="Pilih Kolom Jenis Perairan (Contoh: REMARK / FCODE)", name="field_jns_sungai", datatype="Field", parameterType="Required", direction="Input")
        param_f_jns_sungai.parameterDependencies = [param_sungai.name]

        param_f_sungai = arcpy.Parameter(displayName="Pilih Kolom Nama Perairan (Contoh: NAMOBJ)", name="field_sungai", datatype="Field", parameterType="Required", direction="Input")
        param_f_sungai.parameterDependencies = [param_sungai.name]

        param_pantai = arcpy.Parameter(displayName="Garis Pantai (Polyline) - Opsional", name="in_pantai", datatype="GPFeatureLayer", parameterType="Optional", direction="Input")
        param_pantai.filter.list = ["Polyline"]

        param_teks_pantai = arcpy.Parameter(displayName="Teks Referensi Garis Pantai (Kustom)", name="teks_pantai", datatype="GPString", parameterType="Optional", direction="Input")
        param_teks_pantai.value = "merujuk pada data Garis Pantai Peta Rupa Bumi Indonesia Skala 1:5.000, Tahun 2025"

        param_kab = arcpy.Parameter(displayName="Batas Kabupaten Tetangga (Polygon/Polyline) - Opsional", name="in_kab", datatype="GPFeatureLayer", parameterType="Optional", direction="Input")
        param_kab.filter.list = ["Polygon", "Polyline"]

        param_f_kab = arcpy.Parameter(displayName="Pilih Kolom Nama Kabupaten Tetangga", name="field_kab", datatype="Field", parameterType="Optional", direction="Input")
        param_f_kab.parameterDependencies = [param_kab.name]

        param_teks_kab = arcpy.Parameter(displayName="Teks Referensi Batas Kab (Kustom)", name="teks_kab", datatype="GPString", parameterType="Optional", direction="Input")
        param_teks_kab.value = "sebagaimana diatur dalam Peraturan Menteri Dalam Negeri ..."

        # PARAMETER BARU: Kabupaten Tuan Rumah (Pengecualian)
        param_kab_utama = arcpy.Parameter(displayName="Nama Kabupaten Utama / Tuan Rumah (Pengecualian dari Teks)", name="kab_utama", datatype="GPString", parameterType="Optional", direction="Input")

        param_out = arcpy.Parameter(displayName="Output File Excel", name="out_excel", datatype="DEFile", parameterType="Required", direction="Output")
        param_out.filter.list = ["xls", "xlsx"]

        return [param_simpul, param_biasa, 
                param_segmen, param_f_segmen, 
                param_desa, 
                param_pl, param_f_pl, 
                param_jalan, param_f_jalan, 
                param_sungai, param_f_jns_sungai, param_f_sungai, 
                param_pantai, param_teks_pantai, 
                param_kab, param_f_kab, param_teks_kab, param_kab_utama, 
                param_out]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        return

    def get_arah_mata_angin(self, p1, p2):
        if not p1 or not p2: return "-"
        dx = p2.X - p1.X
        dy = p2.Y - p1.Y
        if dx == 0 and dy == 0: return "-"
        
        degrees = math.degrees(math.atan2(dx, dy))
        if degrees < 0: degrees += 360

        if 337.5 <= degrees or degrees < 22.5: return "utara"
        elif 22.5 <= degrees < 67.5: return "timur laut"
        elif 67.5 <= degrees < 112.5: return "timur"
        elif 112.5 <= degrees < 157.5: return "tenggara"
        elif 157.5 <= degrees < 202.5: return "selatan"
        elif 202.5 <= degrees < 247.5: return "barat daya"
        elif 247.5 <= degrees < 292.5: return "barat"
        elif 292.5 <= degrees < 337.5: return "barat laut"
        return "-"

    def get_field_name(self, fc, possible_names):
        if not fc: return None
        fields = [f.name.lower() for f in arcpy.ListFields(fc)]
        for name in possible_names:
            if name.lower() in fields:
                for f in arcpy.ListFields(fc):
                    if f.name.lower() == name.lower(): return f.name
        return None

    def val_str(self, val):
        if val is None: return "-"
        if type(val).__name__ in ['unicode', 'str']:
            s = val.strip()
        else:
            try: s = str(val).strip()
            except: s = repr(val).strip()
        return s if s and s.lower() != 'nan' else "-"

    def format_coord(self, val):
        if val is None or val == "-": return "-"
        try: return "{:.2f}".format(float(str(val).replace(',', '.')))
        except: return str(val)

    def get_domain_dict(self, fc, field_name):
        mapping = {}
        try:
            if not fc or not field_name: return mapping
            desc = arcpy.Describe(fc)
            if desc.subtypeFieldName and desc.subtypeFieldName.lower() == field_name.lower():
                subtypes = arcpy.da.ListSubtypes(fc)
                for stcode, stdict in subtypes.items():
                    mapping[stcode] = stdict['Name']
                return mapping
            field = next((f for f in desc.fields if f.name.lower() == field_name.lower()), None)
            if field and field.domain:
                ws = desc.path
                while ws:
                    ws_desc = arcpy.Describe(ws)
                    if ws_desc.dataType == "Workspace": break
                    ws = os.path.dirname(ws)
                if ws:
                    domains = arcpy.da.ListDomains(ws)
                    for d in domains:
                        if d.name == field.domain: return d.codedValues
        except: pass
        return mapping

    def get_intersect_data(self, tk_buff, target_fc, target_fields, scratch, domain_maps=None):
        if not target_fc: return {}
        sj_fc = os.path.join(scratch, "sj_temp")
        if arcpy.Exists(sj_fc): arcpy.Delete_management(sj_fc)
        
        # JOIN_ONE_TO_MANY memungkinkan menangkap >1 kabupaten jika titik persis di perbatasan
        arcpy.SpatialJoin_analysis(tk_buff, target_fc, sj_fc, "JOIN_ONE_TO_MANY", "KEEP_ALL", "", "INTERSECT")
        
        res = {}
        if not domain_maps: domain_maps = [{}] * len(target_fields)
        
        actual_fields = [f.name.lower() for f in arcpy.ListFields(sj_fc)]
        valid_fields = ["TK_ID"]
        field_mapping_idx = []
        
        for i, tf in enumerate(target_fields):
            if tf and tf.lower() in actual_fields:
                exact_name = next(f.name for f in arcpy.ListFields(sj_fc) if f.name.lower() == tf.lower())
                valid_fields.append(exact_name)
                field_mapping_idx.append(i)
                
        if len(valid_fields) == 1:
            arcpy.Delete_management(sj_fc)
            return res
            
        try:
            with arcpy.da.SearchCursor(sj_fc, valid_fields) as sc:
                for row in sc:
                    tk_id = row[0]
                    if tk_id is None: continue
                    mapped_vals = []
                    for col_idx in range(1, len(valid_fields)):
                        val = row[col_idx]
                        orig_idx = field_mapping_idx[col_idx-1]
                        d_map = domain_maps[orig_idx]
                        if d_map and val in d_map: mapped_vals.append(d_map[val])
                        else: mapped_vals.append(val)
                        
                    final_vals = []
                    extracted_idx = 0
                    for i in range(len(target_fields)):
                        if i in field_mapping_idx:
                            final_vals.append(mapped_vals[extracted_idx])
                            extracted_idx += 1
                        else:
                            final_vals.append("-")
                    res.setdefault(tk_id, []).append(final_vals)
        except: pass
        arcpy.Delete_management(sj_fc)
        return res

    def execute(self, parameters, messages):
        try:
            in_tk_simpul = parameters[0].valueAsText
            in_tk_biasa  = parameters[1].valueAsText
            in_segmen    = parameters[2].valueAsText
            f_seg_nama   = parameters[3].valueAsText
            in_desa      = parameters[4].valueAsText
            in_pl        = parameters[5].valueAsText
            f_pl_nama    = parameters[6].valueAsText
            in_jalan     = parameters[7].valueAsText
            f_jln_nama   = parameters[8].valueAsText
            in_sungai    = parameters[9].valueAsText
            f_sng_jns    = parameters[10].valueAsText
            f_sng_nama   = parameters[11].valueAsText
            in_pantai    = parameters[12].valueAsText
            teks_pantai_custom = parameters[13].valueAsText or "merujuk pada data Garis Pantai Peta Rupa Bumi Indonesia Skala 1:5.000, Tahun 2025"
            
            in_kab       = parameters[14].valueAsText
            f_kab_nama   = parameters[15].valueAsText
            teks_kab_custom = parameters[16].valueAsText or "sebagaimana diatur dalam Peraturan Menteri Dalam Negeri..."
            
            # Membaca parameter pengecualian Tuan Rumah (Case Insensitive)
            kab_utama_raw = parameters[17].valueAsText
            kab_utama = kab_utama_raw.lower().strip() if kab_utama_raw else ""

            out_excel    = parameters[18].valueAsText

            if not out_excel.lower().endswith(('.xls', '.xlsx')):
                out_excel += '.xls'

            arcpy.env.overwriteOutput = True
            scratch = arcpy.env.scratchGDB
            
            sr = arcpy.Describe(in_tk_simpul).spatialReference
            arcpy.env.outputCoordinateSystem = sr

            arcpy.AddMessage("1. Menggabungkan TK Simpul dan TK Biasa...")
            tk_merged = os.path.join(scratch, "tk_merged")
            if in_tk_biasa: arcpy.Merge_management([in_tk_simpul, in_tk_biasa], tk_merged)
            else: arcpy.CopyFeatures_management(in_tk_simpul, tk_merged)

            arcpy.AddMessage("2. Membaca Domain GDB dan Field Atribut dari Input...")
            dom_seg = self.get_domain_dict(in_segmen, f_seg_nama)
            dom_pl = self.get_domain_dict(in_pl, f_pl_nama)
            dom_jln = self.get_domain_dict(in_jalan, f_jln_nama)
            dom_sng_jns = self.get_domain_dict(in_sungai, f_sng_jns)
            dom_sng_nama = self.get_domain_dict(in_sungai, f_sng_nama)
            dom_kab = self.get_domain_dict(in_kab, f_kab_nama) if in_kab and f_kab_nama else {}

            f_tk_nama = self.get_field_name(tk_merged, ["NAMOBJ", "Nama_TK"]) or "NAMOBJ"
            f_tk_lat = self.get_field_name(tk_merged, ["KOORDY_DMS", "LINTANG"])
            f_tk_lon = self.get_field_name(tk_merged, ["KOORDX_DMS", "BUJUR"])
            f_tk_x = self.get_field_name(tk_merged, ["UTMX", "X"])
            f_tk_y = self.get_field_name(tk_merged, ["UTMY", "Y"])

            f_desa_kd = self.get_field_name(in_desa, ["KDEPUM"]) or "KDEPUM"
            f_desa_nama = self.get_field_name(in_desa, ["NAMOBJ", "Desa"]) or "NAMOBJ"
            f_desa_kec = self.get_field_name(in_desa, ["WADMKC", "Kecamatan"]) or "WADMKC"
            
            dom_desa_nama = self.get_domain_dict(in_desa, f_desa_nama)
            dom_desa_kec = self.get_domain_dict(in_desa, f_desa_kec)

            arcpy.AddMessage("3. Melakukan Analisis Tumpang Susun (Spatial Join)...")
            tk_pts = os.path.join(scratch, "tk_pts_temp")
            arcpy.CopyFeatures_management(tk_merged, tk_pts)
            arcpy.AddField_management(tk_pts, "TK_ID", "LONG")
            arcpy.CalculateField_management(tk_pts, "TK_ID", "!OBJECTID!", "PYTHON_9.3")

            tk_data = {}
            fields_tk = ['TK_ID', 'SHAPE@', f_tk_nama]
            if f_tk_lat: fields_tk.append(f_tk_lat)
            if f_tk_lon: fields_tk.append(f_tk_lon)
            if f_tk_x: fields_tk.append(f_tk_x)
            if f_tk_y: fields_tk.append(f_tk_y)

            with arcpy.da.SearchCursor(tk_pts, fields_tk) as sc:
                for row in sc:
                    tk_data[row[0]] = {
                        'geom': row[1], 'nama': self.val_str(row[2]),
                        'lat': self.val_str(row[3]) if f_tk_lat else "-",
                        'lon': self.val_str(row[4]) if f_tk_lon else "-",
                        'x': self.format_coord(row[5]) if f_tk_x else "-",
                        'y': self.format_coord(row[6]) if f_tk_y else "-"
                    }
            
            keep_f = ['OBJECTID', 'FID', 'SHAPE', 'SHAPE_LENGTH', 'SHAPE_AREA', 'TK_ID']
            drop_f = [f.name for f in arcpy.ListFields(tk_pts) if f.name.upper() not in keep_f and f.type.upper() not in ['OID', 'GEOMETRY']]
            if drop_f:
                try: arcpy.DeleteField_management(tk_pts, drop_f)
                except: pass

            desa_polys = []
            with arcpy.da.SearchCursor(in_desa, [f_desa_nama, "SHAPE@"]) as sc:
                for row in sc:
                    val_d = row[0]
                    if dom_desa_nama and val_d in dom_desa_nama: val_d = dom_desa_nama[val_d]
                    d_str = self.val_str(val_d)
                    if d_str.lower().startswith("desa "): d_str = d_str[5:].strip()
                    if row[1]:
                        desa_polys.append({'nama': d_str.title(), 'geom': row[1]})

            segmen_geom = {}
            with arcpy.da.SearchCursor(in_segmen, ['OID@', f_seg_nama, 'SHAPE@']) as sc:
                for row in sc:
                    s_oid = row[0]
                    val = row[1]
                    geom = row[2]
                    if not geom: continue
                    if dom_seg and val in dom_seg: val = dom_seg[val]
                    raw_seg_name = self.val_str(val)

                    if raw_seg_name == "-" or raw_seg_name.lower().startswith("sungai"):
                        mid_pt = geom.positionAlongLine(0.5, True).firstPoint
                        desa_terhubung = []
                        for p in desa_polys:
                            if p['geom'].distanceTo(mid_pt) <= 2.0:
                                if p['nama'] and p['nama'] != "-":
                                    desa_terhubung.append(p['nama'])
                        desa_terhubung = sorted(list(set(desa_terhubung)))
                        if len(desa_terhubung) >= 2:
                            raw_seg_name = "{0}-{1}".format(desa_terhubung[0], desa_terhubung[1])
                        elif len(desa_terhubung) == 1 and raw_seg_name == "-":
                            raw_seg_name = desa_terhubung[0]

                    segmen_geom[s_oid] = {'nama': raw_seg_name, 'geom': geom}

            sj_segmen = os.path.join(scratch, "sj_segmen")
            arcpy.SpatialJoin_analysis(tk_pts, in_segmen, sj_segmen, "JOIN_ONE_TO_MANY", "KEEP_ALL", "", "WITHIN_A_DISTANCE", "1 Meters")
            
            segmen_dict = {} 
            with arcpy.da.SearchCursor(sj_segmen, ['TARGET_FID', 'JOIN_FID']) as sc:
                for row in sc:
                    tk_id, seg_id = row[0], row[1]
                    if seg_id != -1: segmen_dict.setdefault(seg_id, []).append(tk_id)

            tk_buff = os.path.join(scratch, "tk_buff_temp")
            arcpy.Buffer_analysis(tk_pts, tk_buff, "3 Meters") 

            arcpy.AddMessage("   -> Membaca relasi Desa, Infrastruktur, dan Batas Kabupaten Tetangga...")
            tk_desa = self.get_intersect_data(tk_buff, in_desa, [f_desa_kd, f_desa_nama, f_desa_kec], scratch, [{}, dom_desa_nama, dom_desa_kec])
            tk_pl = self.get_intersect_data(tk_buff, in_pl, [f_pl_nama], scratch, [dom_pl])
            tk_jln = self.get_intersect_data(tk_buff, in_jalan, [f_jln_nama], scratch, [dom_jln])
            tk_sng = self.get_intersect_data(tk_buff, in_sungai, [f_sng_jns, f_sng_nama], scratch, [dom_sng_jns, dom_sng_nama])
            tk_pnt = self.get_intersect_data(tk_buff, in_pantai, ['OID@'], scratch, [{}]) if in_pantai else {}
            
            # Ambil data intersect batas kabupaten
            tk_kab = self.get_intersect_data(tk_buff, in_kab, [f_kab_nama], scratch, [dom_kab]) if in_kab and f_kab_nama else {}

            arcpy.AddMessage("4. Menyusun Deskripsi dan Mengkalkulasi Arah Mata Angin...")
            out_table = os.path.join(scratch, "Tabel_Output_TK")
            if arcpy.Exists(out_table): arcpy.Delete_management(out_table)
            arcpy.CreateTable_management(scratch, "Tabel_Output_TK")
            
            fields_def = [
                ("Segmen", "TEXT", 255, "Segmen"), ("No", "LONG", None, "No"),
                ("Titik_Kart", "TEXT", 255, "Titik_Kart"), ("Lintang", "TEXT", 50, "Lintang"),
                ("Bujur", "TEXT", 50, "Bujur"), ("X", "TEXT", 50, "X"), ("Y", "TEXT", 50, "Y"),
                ("Deskripsi_titik", "TEXT", 1000, "Deskripsi_titik"),
                ("Arah_Mata_Angin", "TEXT", 50, "Arah Mata Angin"),
                ("Deskripsi", "TEXT", 1000, "Deskripsi"), ("No_Urut", "LONG", None, "No Urut")
            ]
            
            for fname, ftype, flen, falias in fields_def:
                if flen: arcpy.AddField_management(out_table, fname, ftype, field_length=flen, field_alias=falias)
                else: arcpy.AddField_management(out_table, fname, ftype, field_alias=falias)

            ins_fields = [fd[0] for fd in fields_def]

            with arcpy.da.InsertCursor(out_table, ins_fields) as ic:
                for seg_id, tk_ids in segmen_dict.items():
                    if seg_id not in segmen_geom: continue
                    
                    s_geom = segmen_geom[seg_id]['geom']
                    nama_segmen_aktif = segmen_geom[seg_id]['nama'] 
                    
                    pts_list = []
                    unique_tks = set(tk_ids)
                    for tid in unique_tks:
                        if tid in tk_data:
                            pt_info = tk_data[tid]
                            raw_pt = pt_info['geom'].firstPoint if hasattr(pt_info['geom'], 'firstPoint') else pt_info['geom']
                            measure = s_geom.measureOnLine(raw_pt)
                            pts_list.append((measure, tid, pt_info, raw_pt))
                    
                    pts_list.sort(key=lambda x: x[0])
                    
                    inner_pts = [p for p in pts_list if not p[2]['nama'].endswith("-000")]
                    if len(inner_pts) >= 2:
                        try:
                            n1 = int(inner_pts[0][2]['nama'].split("-")[-1])
                            n2 = int(inner_pts[-1][2]['nama'].split("-")[-1])
                            if n1 > n2: pts_list.reverse()
                        except: pass
                    
                    for i, item in enumerate(pts_list):
                        no_segmen = i + 1 
                        tid = item[1]
                        pt_info = item[2]
                        raw_pt = item[3]
                        nama_titik = pt_info['nama']
                        
                        jnspl_val = self.val_str(tk_pl[tid][0][0]) if tid in tk_pl else "-"
                        jalan_val = self.val_str(tk_jln[tid][0][0]) if tid in tk_jln else "-"
                        sng_jns_val = self.val_str(tk_sng[tid][0][0]) if tid in tk_sng else "-"
                        sng_nama_val = self.val_str(tk_sng[tid][0][1]) if tid in tk_sng and len(tk_sng[tid][0]) > 1 else "-"
                        is_pantai = tid in tk_pnt
                        
                        # LOGIKA FILTER KABUPATEN TETANGGA (Pengecualian Host)
                        kab_val = "-"
                        if tid in tk_kab:
                            found_kabs = []
                            for kab_match in tk_kab[tid]:
                                k_name = self.val_str(kab_match[0])
                                if k_name != "-" and str(k_name).lower() != "nan":
                                    # Cek apakah nama host ada di string hasil deteksi (case insensitive)
                                    if kab_utama and (kab_utama in str(k_name).lower()):
                                        continue  # Lewati / abaikan kabupaten tuan rumah
                                    found_kabs.append(k_name)
                            
                            # Ambil nama kabupaten pertama yang bukan tuan rumah
                            if found_kabs:
                                kab_val = found_kabs[0]
                        
                        deskripsi_tambahan = ""
                        teks_infrastruktur_simpul = ""
                        
                        # --- LOGIKA PERAIRAN / SUNGAI ---
                        if sng_jns_val != "-" and str(sng_jns_val).lower() != "nan":
                            jns_lower = sng_jns_val.lower()
                            if "sungai" in jns_lower:
                                if sng_nama_val == "-" or str(sng_nama_val).lower() in ["nan", "tanpa nama"]:
                                    nama_sungai_fmt = "sungai"
                                else:
                                    sng_bersih = sng_nama_val.title()
                                    if sng_bersih.lower().startswith("sungai "):
                                        nama_sungai_fmt = sng_bersih
                                    else:
                                        nama_sungai_fmt = "Sungai {}".format(sng_bersih)
                                teks_infrastruktur_simpul = " di {}".format(nama_sungai_fmt)
                                deskripsi_tambahan = "menyusuri {}".format(nama_sungai_fmt)
                            else:
                                if sng_nama_val != "-" and str(sng_nama_val).lower() not in ["nan", "tanpa nama"]:
                                    nama_sungai_fmt = "{} {}".format(jns_lower, sng_nama_val.title())
                                else:
                                    nama_sungai_fmt = jns_lower
                                teks_infrastruktur_simpul = " di {}".format(nama_sungai_fmt)
                                deskripsi_tambahan = "menyusuri {}".format(nama_sungai_fmt)
                        
                        # --- LOGIKA JALAN ---
                        elif jalan_val != "-" and str(jalan_val).lower() != "nan":
                            if jalan_val.lower() == "tanpa nama": 
                                nama_jalan_fmt = "jalan"
                            else: 
                                jalan_bersih = jalan_val.title()
                                if jalan_bersih.lower().startswith("jalan ") or jalan_bersih.lower().startswith("jln "):
                                    nama_jalan_fmt = jalan_bersih
                                else:
                                    nama_jalan_fmt = "Jalan {}".format(jalan_bersih)
                                    
                            teks_infrastruktur_simpul = " di {}".format(nama_jalan_fmt)
                            deskripsi_tambahan = "mengikuti {}".format(nama_jalan_fmt)
                        
                        # --- LOGIKA PL (LAHAN) ---
                        else:
                            if jnspl_val == "-": jnspl_val = "lahan kosong"
                            deskripsi_tambahan = "melewati {}".format(jnspl_val.lower())

                        # --- LOGIKA BATAS KABUPATEN ---
                        teks_batas_kab = ""
                        if kab_val != "-" and str(kab_val).lower() != "nan":
                            nama_kab_fmt = kab_val.title() if kab_val.lower().startswith("kabupaten") else "Kabupaten {}".format(kab_val.title())
                            teks_batas_kab = " yang berbatasan dengan {} {}".format(nama_kab_fmt, teks_kab_custom)

                        # --- PERAKITAN REDAKSI AKHIR ---
                        deskripsi_titik = ""
                        if nama_titik.endswith("-000"):
                            desa_list = tk_desa.get(tid, [])
                            if desa_list:
                                desa_list.sort(key=lambda x: str(x[0]))
                                kec_dict = {}
                                for d in desa_list:
                                    kec = self.val_str(d[2])
                                    desa_nama = self.val_str(d[1])
                                    if not desa_nama.lower().startswith("desa "): desa_nama = "Desa " + desa_nama.title()
                                    kec_dict.setdefault(kec, []).append(desa_nama)
                                
                                kec_groups = []
                                for kec, desas in kec_dict.items():
                                    desas_unik = sorted(list(set(desas))) 
                                    kec_teks = kec.title() if kec.lower().startswith("kecamatan") else "Kecamatan {}".format(kec.title())
                                    kec_groups.append("{0} {1}".format(", ".join(desas_unik), kec_teks))
                                    
                                teks_wilayah = " dan ".join(kec_groups)
                                base_teks_desa = "titik simpul dari {0}{1}".format(teks_wilayah, teks_infrastruktur_simpul)
                            else:
                                base_teks_desa = "titik simpul{0} (Tidak ada data desa)".format(teks_infrastruktur_simpul)
                                
                            # KEMBALI MENGGUNAKAN TEKS PL (YANG TERLETAK DI...) SETIAP SAAT
                            if is_pantai: 
                                teks_tambahan = " yang terletak di garis pantai {}".format(teks_pantai_custom)
                            else: 
                                teks_tambahan = " yang terletak di {}".format(jnspl_val.lower())
                                
                            deskripsi_titik = base_teks_desa + teks_batas_kab + teks_tambahan
                        else:
                            if deskripsi_tambahan.startswith("menyusuri"): deskripsi_titik = deskripsi_tambahan.replace("menyusuri ", "")
                            elif deskripsi_tambahan.startswith("mengikuti"): deskripsi_titik = deskripsi_tambahan.replace("mengikuti ", "")
                            else: deskripsi_titik = jnspl_val.lower()

                        arah = "-"
                        if i < len(pts_list) - 1: arah = self.get_arah_mata_angin(raw_pt, pts_list[i+1][3])

                        row_vals = (
                            nama_segmen_aktif, no_segmen, nama_titik,
                            pt_info['lat'], pt_info['lon'], pt_info['x'], pt_info['y'],
                            deskripsi_titik, arah, deskripsi_tambahan, no_segmen
                        )
                        ic.insertRow(row_vals)

            arcpy.AddMessage("5. Mengekspor Data ke Excel...")
            try: arcpy.TableToExcel_conversion(out_table, out_excel, "ALIAS")
            except: arcpy.TableToExcel_conversion(out_table, out_excel)
            
            for temp_fc in [tk_merged, tk_pts, tk_buff, out_table, sj_segmen]:
                try: arcpy.Delete_management(temp_fc)
                except: pass

            arcpy.AddMessage("SELESAI! Laporan Excel berhasil dibuat di:\n{}".format(out_excel))

        except arcpy.ExecuteError:
            arcpy.AddError("GEOPROCESSING ERROR:")
            arcpy.AddError(arcpy.GetMessages(2))
        except Exception as e:
            arcpy.AddError("PYTHON ERROR:")
            arcpy.AddError(str(e))
            arcpy.AddError(traceback.format_exc())

# =========================================================
# TOOL 6: BUAT & VALIDASI TOPOLOGI (ATRIBUT 9 LN & 2 PT)
# =========================================================
class TopologiBatasDesa(object):
    def __init__(self):
        self.label = "6. Topology (Line & TK)"
        self.description = "Memvalidasi topologi dan memberikan 9 kolom status untuk Garis Batas Desa dan 2 kolom untuk Titik Kartometrik."
        self.canRunInBackground = False

    def getParameterInfo(self):
        param_out_folder = arcpy.Parameter(
            displayName="Pilih Folder Output",
            name="out_folder",
            datatype="DEFolder",
            parameterType="Required",
            direction="Input")

        param_line = arcpy.Parameter(
            displayName="Input Batas Desa (Garis)",
            name="line_feature",
            datatype="DEFeatureClass",
            parameterType="Required",
            direction="Input")
        param_line.filter.list = ["Polyline"]

        param_point = arcpy.Parameter(
            displayName="Input Titik Kartometrik (Titik)",
            name="point_feature",
            datatype="DEFeatureClass",
            parameterType="Required",
            direction="Input")
        param_point.filter.list = ["Point"]

        return [param_out_folder, param_line, param_point]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        return

    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True

        out_folder = parameters[0].valueAsText
        in_line = parameters[1].valueAsText
        in_point = parameters[2].valueAsText

        gdb_name = "DataTopologiBatas.gdb"
        dataset_name = "Dataset_Batas"
        out_line_name = "Batas_Desa_LN"
        out_point_name = "Titik_Kartometrik_PT"
        topo_name = "Topologi_Batas"

        gdb_path = os.path.join(out_folder, gdb_name)
        dataset_path = os.path.join(gdb_path, dataset_name)
        topo_path = os.path.join(dataset_path, topo_name)

        try:
            arcpy.AddMessage("1. Mempersiapkan Geodatabase di folder output...")
            if arcpy.Exists(gdb_path):
                arcpy.Delete_management(gdb_path)
            
            arcpy.CreateFileGDB_management(out_folder, gdb_name)

            arcpy.AddMessage("2. Membaca sistem koordinat dan membuat Feature Dataset...")
            spatial_ref = arcpy.Describe(in_line).spatialReference
            arcpy.CreateFeatureDataset_management(gdb_path, dataset_name, spatial_ref)

            arcpy.AddMessage("3. Menyalin data Garis dan Titik ke dalam Geodatabase...")
            arcpy.FeatureClassToFeatureClass_conversion(in_line, dataset_path, out_line_name)
            arcpy.FeatureClassToFeatureClass_conversion(in_point, dataset_path, out_point_name)

            fc_line_new = os.path.join(dataset_path, out_line_name)
            fc_point_new = os.path.join(dataset_path, out_point_name)

            arcpy.AddMessage("4. Membuat file Topologi...")
            arcpy.CreateTopology_management(dataset_path, topo_name)

            arcpy.AddMessage("5. Mendaftarkan data ke dalam Topologi...")
            arcpy.AddFeatureClassToTopology_management(topo_path, fc_line_new, 1, 1)
            arcpy.AddFeatureClassToTopology_management(topo_path, fc_point_new, 1, 1)

            arcpy.AddMessage("6. Memasukkan 10 Aturan Topologi...")
            line_rules = [
                "Must Not Overlap (Line)",
                "Must Not Intersect (Line)",
                "Must Not Self-Overlap (Line)",
                "Must Not Self-Intersect (Line)",
                "Must Not Have Dangles (Line)",
                "Must Not Intersect Or Touch Interior (Line)",
                "Must Be Single Part (Line)"
            ]
            for rule in line_rules:
                arcpy.AddRuleToTopology_management(topo_path, rule, fc_line_new)

            arcpy.AddRuleToTopology_management(topo_path, "Must Be Disjoint (Point)", fc_point_new)
            arcpy.AddRuleToTopology_management(topo_path, "Must Be Covered By (Point-Line)", fc_point_new, "", fc_line_new)
            arcpy.AddRuleToTopology_management(topo_path, "Endpoint Must Be Covered By (Line-Point)", fc_line_new, "", fc_point_new)

            arcpy.AddMessage("7. Memvalidasi topologi (mencari error)...")
            arcpy.ValidateTopology_management(topo_path, "Full_Extent")

            arcpy.AddMessage("8. Membedah jenis error...")
            arcpy.ExportTopologyErrors_management(topo_path, gdb_path, "Daftar_Error")

            line_errors = {}
            point_errors = {}

            error_types = ["Daftar_Error_point", "Daftar_Error_line", "Daftar_Error_poly"]
            for err_fc in error_types:
                err_path = os.path.join(gdb_path, err_fc)
                if arcpy.Exists(err_path):
                    fields = [f.name for f in arcpy.ListFields(err_path)]
                    cursor_fields = ["OriginObjectClassName", "OriginObjectID", "DestinationObjectClassName", "DestinationObjectID", "RuleDescription"]
                    
                    valid_fields = [f for f in cursor_fields if f in fields]
                    
                    if "OriginObjectID" in valid_fields and "RuleDescription" in valid_fields:
                        with arcpy.da.SearchCursor(err_path, valid_fields) as cursor:
                            for row in cursor:
                                row_dict = dict(zip(valid_fields, row))
                                rule_desc = str(row_dict.get("RuleDescription", "")).lower()
                                
                                ori_cls = row_dict.get("OriginObjectClassName")
                                ori_id = row_dict.get("OriginObjectID")
                                dest_cls = row_dict.get("DestinationObjectClassName")
                                dest_id = row_dict.get("DestinationObjectID")

                                rule_key = None
                                if "endpoint" in rule_desc: rule_key = "EndpointCover"
                                elif "self-overlap" in rule_desc or "self overlap" in rule_desc: rule_key = "SelfOverlap"
                                elif "self-intersect" in rule_desc or "self intersect" in rule_desc: rule_key = "SelfIntersect"
                                elif "touch interior" in rule_desc: rule_key = "TouchInterior"
                                elif "intersect" in rule_desc: rule_key = "Intersect" 
                                elif "overlap" in rule_desc: rule_key = "Overlap"     
                                elif "dangle" in rule_desc: rule_key = "Dangle"
                                elif "single part" in rule_desc or "multipart" in rule_desc: rule_key = "SinglePart"
                                elif "disjoint" in rule_desc: rule_key = "Disjoint"
                                elif "covered by" in rule_desc: rule_key = "PointCover"
                                
                                if rule_key:
                                    if ori_cls == out_line_name and ori_id:
                                        if ori_id not in line_errors: line_errors[ori_id] = set()
                                        line_errors[ori_id].add(rule_key)
                                    if ori_cls == out_point_name and ori_id:
                                        if ori_id not in point_errors: point_errors[ori_id] = set()
                                        point_errors[ori_id].add(rule_key)

                                    if dest_cls == out_line_name and dest_id:
                                        if dest_id not in line_errors: line_errors[dest_id] = set()
                                        line_errors[dest_id].add(rule_key)
                                    if dest_cls == out_point_name and dest_id:
                                        if dest_id not in point_errors: point_errors[dest_id] = set()
                                        point_errors[dest_id].add(rule_key)

            arcpy.AddMessage("9. Menyiapkan 9 Atribut Garis dan 2 Atribut Titik...")
            
            line_fields_dict = {
                "T_Overlap": ("Must Not Overlap", "Overlap"),
                "T_Intersect": ("Must Not Intersect", "Intersect"),
                "T_SelfOlap": ("Must Not Self-Overlap", "SelfOverlap"),
                "T_SelfInt": ("Must Not Self-Intersect", "SelfIntersect"),
                "T_Dangle": ("Must Not Have Dangles", "Dangle"),
                "T_IntTouch": ("Int or Touch Int", "TouchInterior"),
                "T_SnglPart": ("Must Be Single Part", "SinglePart"),
                "T_PtCoverL": ("Pt Covered By Line", "PointCover"),
                "T_EndCover": ("Endpt Covered By Pt", "EndpointCover")
            }
            
            point_fields_dict = {
                "T_Disjoint": ("Must Be Disjoint", "Disjoint"),
                "T_PtCoverP": ("Pt Covered By Line", "PointCover")
            }

            for f_name, f_data in line_fields_dict.items():
                arcpy.AddField_management(fc_line_new, f_name, "TEXT", field_length=12, field_alias=f_data[0])
                
            for f_name, f_data in point_fields_dict.items():
                arcpy.AddField_management(fc_point_new, f_name, "TEXT", field_length=12, field_alias=f_data[0])

            arcpy.AddMessage("10. Membuka sesi pengeditan & menyuntikkan status...")
            edit = arcpy.da.Editor(gdb_path)
            edit.startEditing(False, True)
            edit.startOperation()

            arcpy.AddMessage("   - Memperbarui 9 atribut Batas Desa (Garis)...")
            line_field_names = list(line_fields_dict.keys())
            cursor_fields_line = ["OID@"] + line_field_names
            with arcpy.da.UpdateCursor(fc_line_new, cursor_fields_line) as cursor:
                for row in cursor:
                    oid = row[0]
                    err_set = line_errors.get(oid, set())
                    
                    for i, col_name in enumerate(line_field_names, start=1):
                        rule_key = line_fields_dict[col_name][1]
                        if rule_key in err_set:
                            row[i] = "Cek Lagi"
                        else:
                            row[i] = "Clear"
                    cursor.updateRow(row)

            arcpy.AddMessage("   - Memperbarui 2 atribut Titik Kartometrik (Titik)...")
            point_field_names = list(point_fields_dict.keys())
            cursor_fields_point = ["OID@"] + point_field_names
            with arcpy.da.UpdateCursor(fc_point_new, cursor_fields_point) as cursor:
                for row in cursor:
                    oid = row[0]
                    err_set = point_errors.get(oid, set())
                    
                    for i, col_name in enumerate(point_field_names, start=1):
                        rule_key = point_fields_dict[col_name][1]
                        if rule_key in err_set:
                            row[i] = "Cek Lagi"
                        else:
                            row[i] = "Clear"
                    cursor.updateRow(row)

            edit.stopOperation()
            edit.stopEditing(True)

            arcpy.AddMessage("\nPROSES SELESAI!")
            arcpy.AddMessage("Buka Attribute Table di ArcMap. Garis Anda kini memiliki 9 kolom tambahan dan Titik memiliki 2 kolom!")

        except arcpy.ExecuteError:
            arcpy.AddError(arcpy.GetMessages(2))
        except Exception as e:
            arcpy.AddError(str(e))
            
        return

# =========================================================
# TOOL 7: PROSES VALIDASI TOPOLOGI (MATRIKS TABEL)
# =========================================================
class ValidasiTopologiDesa(object):
    def __init__(self):
        self.label = "7. Topology 13 Rules (All Data)"
        self.description = "Mengeksekusi topologi dengan 13 rules pada total jenis data yang ada."
        self.canRunInBackground = False

    def getParameterInfo(self):
        param_folder = arcpy.Parameter(displayName="Folder Penyimpanan Output", name="out_folder", datatype="DEFolder", parameterType="Required", direction="Input")
        param_gdb = arcpy.Parameter(displayName="Nama File Geodatabase (.gdb)", name="gdb_name", datatype="GPString", parameterType="Required", direction="Input")
        param_gdb.value = "Validasi_Batas_Desa.gdb"

        param_A = arcpy.Parameter(displayName="(A) Input: Batas Desa (Line)", name="fc_A", datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        param_A.filter.list = ["Polyline"]

        param_B = arcpy.Parameter(displayName="(B) Input: Batas Desa (Polygon)", name="fc_B", datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        param_B.filter.list = ["Polygon"]

        param_C = arcpy.Parameter(displayName="(C) Input: Informasi Lainnya (Point)", name="fc_C", datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        param_C.filter.list = ["Point"]

        param_D = arcpy.Parameter(displayName="(D) Input: Titik Kartometrik (Point)", name="fc_D", datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        param_D.filter.list = ["Point"]

        param_E = arcpy.Parameter(displayName="(E) Input: Garis Pantai (Line)", name="fc_E", datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        param_E.filter.list = ["Polyline"]

        return [param_folder, param_gdb, param_A, param_B, param_C, param_D, param_E]

    def isLicensed(self):
        if arcpy.CheckProduct("ArcInfo") == "AlreadyInitialized" or arcpy.CheckProduct("ArcEditor") == "AlreadyInitialized":
            return True
        return False

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        return

    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True 
        
        out_folder = parameters[0].valueAsText
        gdb_name = parameters[1].valueAsText
        in_fc_A = parameters[2].valueAsText
        in_fc_B = parameters[3].valueAsText
        in_fc_C = parameters[4].valueAsText
        in_fc_D = parameters[5].valueAsText
        in_fc_E = parameters[6].valueAsText

        if not gdb_name.lower().endswith(".gdb"):
            gdb_name += ".gdb"
        gdb_path = os.path.join(out_folder, gdb_name)

        if arcpy.Exists(gdb_path):
            arcpy.AddMessage("Menghapus Geodatabase lama...")
            try:
                arcpy.Delete_management(gdb_path)
            except Exception as e:
                arcpy.AddError("GAGAL MENGHAPUS GDB! Pastikan file GDB tidak sedang Anda buka di dalam ArcMap. Detail: " + str(e))
                return
            
        arcpy.AddMessage("Membuat Geodatabase baru...")
        arcpy.CreateFileGDB_management(out_folder, gdb_name)

        arcpy.AddMessage("Membuat Feature Dataset dan menyalin layer Anda...")
        spatial_ref = arcpy.Describe(in_fc_A).spatialReference
        fd_name = "FD_Topologi"
        fd_path = os.path.join(gdb_path, fd_name)
        arcpy.CreateFeatureDataset_management(gdb_path, fd_name, spatial_ref)

        inputs = {
            "A_Batas_Desa_LN": in_fc_A,
            "B_Batas_Desa_AR": in_fc_B,
            "C_Info_Lainnya_PT": in_fc_C,
            "D_Titik_Kartometrik_PT": in_fc_D,
            "E_Garis_Pantai_LN": in_fc_E
        }
        
        out_fcs = {}
        for out_name, in_fc in inputs.items():
            out_fc_path = os.path.join(fd_path, out_name)
            arcpy.CopyFeatures_management(in_fc, out_fc_path)
            out_fcs[out_name] = out_fc_path

        fc_A = out_fcs["A_Batas_Desa_LN"]
        fc_B = out_fcs["B_Batas_Desa_AR"]
        fc_C = out_fcs["C_Info_Lainnya_PT"]
        fc_D = out_fcs["D_Titik_Kartometrik_PT"]
        fc_E = out_fcs["E_Garis_Pantai_LN"]

        rules_mapping = [
            ("Must Not Overlap (Line)", fc_A, None, "R_Overlap", "Rule: Must Not Overlap", [fc_A]),
            ("Must Not Intersect (Line)", fc_A, None, "R_Intersect", "Rule: Must Not Intersect", [fc_A]),
            ("Must Not Self-Overlap (Line)", fc_A, None, "R_Self_Ovr", "Rule: Must Not Self-Overlap", [fc_A]),
            ("Must Not Self-Intersect (Line)", fc_A, None, "R_Self_Int", "Rule: Must Not Self-Intersect", [fc_A]),
            ("Must Not Have Dangles (Line)", fc_A, None, "R_Dangle", "Rule: Must Not Have Dangles", [fc_A]),
            ("Must Not Intersect Or Touch Interior (Line)", fc_A, None, "R_Touch_Int", "Rule: No Intersect/Touch Interior", [fc_A]),
            ("Must Be Single Part (Line)", fc_A, None, "R_Sngl_Part", "Rule: Must Be Single Part", [fc_A]),
            
            ("Must Not Have Gaps (Area)", fc_B, None, "R_Gap", "Rule: Must Not Have Gaps", [fc_B]),
            ("Must Not Overlap (Area)", fc_B, None, "R_Overlap", "Rule: Must Not Overlap", [fc_B]),
            
            ("Must Be Covered By Boundary Of (Line-Area)", fc_A, fc_B, "R_Cov_Bndry", "Rule: Covered By Boundary Of", [fc_A, fc_B]),
            ("Must Be Covered By Boundary Of (Line-Area)", fc_E, fc_B, "R_Cov_Bndry", "Rule: Covered By Boundary Of", [fc_E, fc_B]),
            
            ("Contains One Point (Area-Point)", fc_B, fc_C, "R_Cont_Pt", "Rule: Contains One Point", [fc_B, fc_C]),
            
            ("Must Be Covered By (Point-Line)", fc_D, fc_A, "R_Cov_Line", "Rule: Point Covered By Line", [fc_D, fc_A]),
            
            ("Must Be Disjoint (Point)", fc_C, None, "R_Disjoint", "Rule: Must Be Disjoint", [fc_C]),
            ("Must Be Disjoint (Point)", fc_D, None, "R_Disjoint", "Rule: Must Be Disjoint", [fc_D])
        ]

        arcpy.AddMessage("Menyiapkan kolom atribut (clear/cek lagi) untuk tiap aturan...")
        fc_fields_dict = {}
        for rule_str, orig_fc, dest_fc, fld_name, fld_alias, target_fcs in rules_mapping:
            for t_fc in target_fcs:
                if t_fc not in fc_fields_dict:
                    fc_fields_dict[t_fc] = {}
                fc_fields_dict[t_fc][fld_name] = fld_alias
            
        for fc_path, fields in fc_fields_dict.items():
            for fld_name, fld_alias in fields.items():
                arcpy.AddField_management(fc_path, fld_name, "TEXT", field_length=15, field_alias=fld_alias)
                arcpy.CalculateField_management(fc_path, fld_name, '"clear"', "PYTHON_9.3")

        arcpy.AddMessage("Membuat Topologi dan menyematkan aturan...")
        topo_name = "Topologi_Batas_Desa"
        topology = arcpy.CreateTopology_management(fd_path, topo_name)[0]

        for fc_path in out_fcs.values():
            arcpy.AddFeatureClassToTopology_management(topology, fc_path, 1, 1)

        for rule, origin_fc, dest_fc, fld_name, fld_alias, target_fcs in rules_mapping:
            if dest_fc is None:
                arcpy.AddRuleToTopology_management(topology, rule, origin_fc)
            else:
                arcpy.AddRuleToTopology_management(topology, rule, origin_fc, "#", dest_fc)

        arcpy.AddMessage("Memvalidasi Topologi...")
        arcpy.ValidateTopology_management(topology, "Full_Extent")

        arcpy.AddMessage("Mengekstrak tabel Error Topologi...")
        err_basename = "ErrorTopo"
        arcpy.ExportTopologyErrors_management(topology, gdb_path, err_basename)
        
        err_pts = os.path.join(gdb_path, err_basename + "_point")
        err_lns = os.path.join(gdb_path, err_basename + "_line")
        err_pls = os.path.join(gdb_path, err_basename + "_poly")

        def map_desc_to_field(desc):
            d = desc.lower()
            if "self-overlap" in d or "self overlap" in d: return "R_Self_Ovr"
            if "self-intersect" in d or "self intersect" in d: return "R_Self_Int"
            if "overlap" in d: return "R_Overlap"
            if "touch interior" in d: return "R_Touch_Int"
            if "intersect" in d: return "R_Intersect"
            if "dangle" in d: return "R_Dangle"
            if "single part" in d: return "R_Sngl_Part"
            if "gap" in d: return "R_Gap"
            if "boundary" in d: return "R_Cov_Bndry"
            if "contains one" in d: return "R_Cont_Pt"
            if "covered by" in d: return "R_Cov_Line"
            if "disjoint" in d: return "R_Disjoint"
            return None

        arcpy.AddMessage("Memetakan Object ID spesifik ke kolom masing-masing...")
        error_dict = {}
        gap_geometries = []

        for err_fc in [err_pts, err_lns, err_pls]:
            if arcpy.Exists(err_fc) and int(arcpy.GetCount_management(err_fc)[0]) > 0:
                with arcpy.da.SearchCursor(err_fc, ["OriginObjectClassName", "OriginObjectID", "DestinationObjectClassName", "DestinationObjectID", "RuleDescription", "SHAPE@"]) as cur:
                    for row in cur:
                        orig_cls = row[0]
                        orig_oid = row[1]
                        dest_cls = row[2]
                        dest_oid = row[3]
                        desc = row[4]
                        shape = row[5]
                        
                        fld = map_desc_to_field(desc)
                        if fld:
                            if fld == "R_Gap" and orig_oid <= 0 and shape:
                                gap_geometries.append(shape)
                            else:
                                if orig_cls and orig_oid > 0:
                                    key_orig = (orig_cls, fld)
                                    if key_orig not in error_dict: error_dict[key_orig] = set()
                                    error_dict[key_orig].add(orig_oid)
                                    
                                if dest_cls and dest_oid > 0:
                                    key_dest = (dest_cls, fld)
                                    if key_dest not in error_dict: error_dict[key_dest] = set()
                                    error_dict[key_dest].add(dest_oid)

        arcpy.AddMessage("Memperbarui status 'cek lagi' pada tabel atribut Anda...")
        
        edit = arcpy.da.Editor(gdb_path)
        edit.startEditing(True, False) 
        edit.startOperation()

        try:
            for out_name, fc_path in out_fcs.items():
                if fc_path not in fc_fields_dict:
                    continue
                    
                flds_list = list(fc_fields_dict[fc_path].keys())
                oid_name = arcpy.Describe(fc_path).OIDFieldName
                
                update_fields = [oid_name, "SHAPE@"] + flds_list
                
                with arcpy.da.UpdateCursor(fc_path, update_fields) as cur:
                    for row in cur:
                        oid = row[0]
                        shape = row[1]
                        updated = False
                        
                        for i, fld in enumerate(flds_list):
                            col_idx = i + 2
                            key = (out_name, fld)
                            
                            if key in error_dict and oid in error_dict[key]:
                                row[col_idx] = "cek lagi"
                                updated = True
                                
                            elif fld == "R_Gap" and shape and gap_geometries:
                                for gap_geom in gap_geometries:
                                    if not shape.disjoint(gap_geom): 
                                        row[col_idx] = "cek lagi"
                                        updated = True
                                        break
                        
                        if updated:
                            cur.updateRow(row)
                            
            edit.stopOperation()
            edit.stopEditing(True) 

        except Exception as e:
            edit.stopOperation()
            edit.stopEditing(False) 
            arcpy.AddError("Gagal saat mengupdate tabel: " + str(e))
            raise e

        arcpy.AddMessage("PROSES SELESAI! Silakan buka tabel atribut masing-masing layer untuk melihat hasilnya.")
        return

# =====================================================================
# TOOL 8: BATCH EXPORT MXD
# =====================================================================
class BatchExportMXD(object):
    def __init__(self):
        self.label = "8. Batch Export MXD"
        self.description = "Melakukan export MXD & APRX secara masal dengan opsi format file beserta DPI."
        self.canRunInBackground = False

    def getParameterInfo(self):
        param_in_folder = arcpy.Parameter(displayName="Folder Sumber Dokumen Peta", name="in_folder", datatype="DEFolder", parameterType="Required", direction="Input")
        
        param_recursive = arcpy.Parameter(displayName="Pindai Seluruh Subfolder di Dalamnya (Rekursif)?", name="recursive", datatype="GPBoolean", parameterType="Optional", direction="Input")
        param_recursive.value = False

        param_formats = arcpy.Parameter(displayName="Format Ekspor", name="export_formats", datatype="GPString", parameterType="Required", direction="Input", multiValue=True)
        param_formats.filter.type = "ValueList"
        param_formats.filter.list = ["PDF", "JPEG", "PNG", "TIFF"]
        param_formats.value = ["PDF", "JPEG"]

        param_dpi = arcpy.Parameter(displayName="Resolusi Ekspor (DPI)", name="dpi_setting", datatype="GPLong", parameterType="Required", direction="Input")
        param_dpi.value = 300

        param_out_folder = arcpy.Parameter(displayName="Folder Tujuan Output", name="out_folder", datatype="DEFolder", parameterType="Required", direction="Input")

        param_keep_structure = arcpy.Parameter(displayName="Pertahankan Struktur Subfolder pada Output?", name="keep_subfolders", datatype="GPBoolean", parameterType="Optional", direction="Input")
        param_keep_structure.value = True

        return [param_in_folder, param_recursive, param_formats, param_dpi, param_out_folder, param_keep_structure]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        if parameters[3].value is not None and parameters[3].value < 72:
            parameters[3].setErrorMessage("Nilai DPI minimal adalah 72.")
        return

    def _safe_makedirs(self, folder_path):
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)

    def _export_desktop(self, mxd_path, out_base_path, formats, dpi):
        mxd = arc_mapping.MapDocument(mxd_path)
        try:
            for fmt in formats:
                out_file = "{}.{}".format(out_base_path, fmt.lower())
                if fmt == "PDF":
                    arc_mapping.ExportToPDF(mxd, out_file, resolution=dpi)
                elif fmt == "JPEG":
                    arc_mapping.ExportToJPEG(mxd, out_file, resolution=dpi)
                elif fmt == "PNG":
                    arc_mapping.ExportToPNG(mxd, out_file, resolution=dpi)
                elif fmt == "TIFF":
                    arc_mapping.ExportToTIFF(mxd, out_file, resolution=dpi)
        finally:
            del mxd

    def _export_pro(self, doc_path, out_base_path, formats, dpi, scratch_dir):
        ext = os.path.splitext(doc_path)[1].lower()
        aprx = None
        temp_aprx_created = False
        try:
            if ext == ".aprx":
                aprx = arc_mapping.ArcGISProject(doc_path)
            elif ext == ".mxd":
                temp_aprx_path = os.path.join(scratch_dir, "temp_import.aprx")
                if os.path.exists(temp_aprx_path):
                    os.remove(temp_aprx_path)
                try:
                    current_aprx = arc_mapping.ArcGISProject("CURRENT")
                    current_aprx.saveACopy(temp_aprx_path)
                    aprx = arc_mapping.ArcGISProject(temp_aprx_path)
                    temp_aprx_created = True
                except:
                    default_template = os.path.join(arcpy.GetInstallInfo()["InstallDir"], "Resources", "NewProjectTemplates", "Blank.aptx")
                    aprx = arc_mapping.ArcGISProject(default_template)
                aprx.importDocument(doc_path)

            layouts = aprx.listLayouts()
            if not layouts:
                arcpy.AddWarning("  -> Dilewati: Tidak ditemukan Layout pada {}".format(os.path.basename(doc_path)))
                return

            for lyt in layouts:
                suffix = "_{}".format(lyt.name) if len(layouts) > 1 else ""
                for fmt in formats:
                    out_file = "{}{}.{}".format(out_base_path, suffix, fmt.lower())
                    if fmt == "PDF":
                        lyt.exportToPDF(out_file, resolution=dpi)
                    elif fmt == "JPEG":
                        lyt.exportToJPEG(out_file, resolution=dpi)
                    elif fmt == "PNG":
                        lyt.exportToPNG(out_file, resolution=dpi)
                    elif fmt == "TIFF":
                        lyt.exportToTIFF(out_file, resolution=dpi)
        finally:
            if aprx:
                del aprx
            if temp_aprx_created and os.path.exists(temp_aprx_path):
                try:
                    os.remove(temp_aprx_path)
                except:
                    pass

    def execute(self, parameters, messages):
        in_folder = parameters[0].valueAsText
        recursive = bool(parameters[1].value)
        formats = parameters[2].valueAsText.replace("'", "").replace('"', "").split(";")
        dpi = int(parameters[3].value)
        out_folder = parameters[4].valueAsText
        keep_structure = bool(parameters[5].value)

        scratch_dir = arcpy.env.scratchFolder
        engine_name = "ArcGIS Pro (arcpy.mp)" if IS_PRO else "ArcGIS Desktop (arcpy.mapping)"
        arcpy.AddMessage("Menjalankan ekspor menggunakan engine: {}".format(engine_name))

        valid_exts = [".mxd", ".aprx"] if IS_PRO else [".mxd"]

        doc_files = []
        if recursive:
            for root, dirs, files in os.walk(in_folder):
                for f in files:
                    if os.path.splitext(f)[1].lower() in valid_exts:
                        doc_files.append(os.path.join(root, f))
        else:
            for f in os.listdir(in_folder):
                full_path = os.path.join(in_folder, f)
                if os.path.isfile(full_path) and os.path.splitext(f)[1].lower() in valid_exts:
                    doc_files.append(full_path)

        total_files = len(doc_files)
        if total_files == 0:
            arcpy.AddWarning("Tidak ditemukan file dokumen peta ({}) pada folder yang dipilih.".format(", ".join(valid_exts)))
            return

        arcpy.AddMessage("Ditemukan {} file untuk diekspor ke format: {} (DPI: {})".format(total_files, ", ".join(formats), dpi))
        arcpy.SetProgressor("step", "Mengekspor dokumen peta...", 0, total_files, 1)

        success_count = 0
        failed_count = 0

        for idx, doc_path in enumerate(doc_files, 1):
            file_name = os.path.basename(doc_path)
            base_name = os.path.splitext(file_name)[0]
            arcpy.SetProgressorLabel("Memproses ({}/{}): {}".format(idx, total_files, file_name))
            arcpy.AddMessage("[{}/{}] Memproses: {}".format(idx, total_files, file_name))

            if keep_structure and recursive:
                rel_dir = os.path.relpath(os.path.dirname(doc_path), in_folder)
                target_dir = os.path.join(out_folder, rel_dir)
            else:
                target_dir = out_folder

            self._safe_makedirs(target_dir)
            out_base = os.path.join(target_dir, base_name)

            try:
                if IS_PRO:
                    self._export_pro(doc_path, out_base, formats, dpi, scratch_dir)
                else:
                    self._export_desktop(doc_path, out_base, formats, dpi)
                success_count += 1
            except Exception as e:
                arcpy.AddError("  -> Gagal mengekspor {}: {}".format(file_name, str(e)))
                failed_count += 1

            arcpy.SetProgressorPosition()

        arcpy.ResetProgressor()
        arcpy.AddMessage("Selesai! Berhasil: {} file | Gagal: {} file.".format(success_count, failed_count))
