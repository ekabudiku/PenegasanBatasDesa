import arcpy
import os
import math
import re
import sys
import traceback

class Toolbox(object):
    def __init__(self):
        self.label = "Toolkit Batas Desa"
        self.alias = "batas_desa_toolkit"
        # Daftar tool yang akan muncul berurutan di ArcGIS
        self.tools = [
            GenerateTK, 
            AutoNameTKBiasa, 
            QCTitikKartometrik, 
            ExportTKExcel, 
            ExportLaporanTK
        ]

# =========================================================
# TOOL 1: GENERATE TK SIMPUL
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
        """Fungsi bantuan untuk mengonversi Decimal Degree ke format DMS (String)"""
        direction = ""
        if is_lat:
            direction = "N" if dd >= 0 else "S"
        else:
            direction = "E" if dd >= 0 else "W"
        
        dd = abs(dd)
        degrees = int(dd)
        minutes = int((dd - degrees) * 60)
        seconds = round((((dd - degrees) * 60) - minutes) * 60, 3)
        
        return u"{0}\u00B0 {1}' {2}\" {3}".format(degrees, minutes, seconds, direction)

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
            
            # FIX: Salin data dulu untuk memutus ikatan Topology
            arcpy.AddMessage("   -> Menyalin sementara data untuk memutus ikatan Topologi...")
            temp_copy = os.path.join(scratch, "temp_tk_poly_copy")
            arcpy.CopyFeatures_management(in_fc, temp_copy)
            
            arcpy.AddMessage("   -> Proyeksi on-the-fly ke UTM Zone {0}...".format(utm_zone))
            work_fc = os.path.join(scratch, "temp_tk_poly_utm")
            arcpy.Project_management(temp_copy, work_fc, work_sr)
            
            # Hapus data salinan awal
            try:
                arcpy.Delete_management(temp_copy)
            except:
                pass
        else:
            work_fc = os.path.join(scratch, "temp_tk_poly")
            work_sr = sr
            arcpy.CopyFeatures_management(in_fc, work_fc)

        arcpy.AddMessage("   -> Menyatukan poligon yang renggang (Toleransi 3 Meter)...")
        arcpy.Integrate_management(work_fc, "3 Meters")

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
                shared_by = vertex_dict[coord]
                
                if len(shared_by) >= 3:
                    tk_coords.add(coord)
                elif len(shared_by) == 2:
                    prev_idx = (n - 2) if i == 0 else (i - 1)
                    next_idx = (i + 1)
                    
                    prev_coord = pts[prev_idx]
                    next_coord = pts[next_idx]
                    
                    prev_shared = vertex_dict[prev_coord]
                    next_shared = vertex_dict[next_coord]
                    
                    if len(prev_shared) < 2 or len(next_shared) < 2:
                        tk_coords.add(coord)

        with arcpy.da.SearchCursor(work_fc, ['SHAPE@']) as cursor:
            for row in cursor:
                geom = row[0]
                if not geom: continue
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
        
        arcpy.AddField_management(temp_out, "Nama_TK", "TEXT", field_length=255)
        arcpy.AddField_management(temp_out, "X", "DOUBLE")
        arcpy.AddField_management(temp_out, "Y", "DOUBLE")
        arcpy.AddField_management(temp_out, "Lintang", "TEXT", field_length=50)
        arcpy.AddField_management(temp_out, "Bujur", "TEXT", field_length=50)

        with arcpy.da.InsertCursor(temp_out, ['SHAPE@', 'Nama_TK', 'X', 'Y', 'Lintang', 'Bujur']) as ic:
            for coord in tk_coords:
                kdepum_set = vertex_dict[coord]
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
                
                x_utm = pt_geom.firstPoint.X
                y_utm = pt_geom.firstPoint.Y
                
                pt_gcs = pt_geom.projectAs(base_gcs)
                lon_dd = pt_gcs.firstPoint.X
                lat_dd = pt_gcs.firstPoint.Y
                
                lintang_str = self.dd_to_dms(lat_dd, is_lat=True)
                bujur_str = self.dd_to_dms(lon_dd, is_lat=False)
                
                ic.insertRow([pt_geom, name, x_utm, y_utm, lintang_str, bujur_str])

        arcpy.AddMessage("5. Mengembalikan Proyeksi dan Membersihkan Data...")
        if sr.type == "Geographic":
            arcpy.Project_management(temp_out, out_fc, sr)
        else:
            arcpy.CopyFeatures_management(temp_out, out_fc)

        for fc in [work_fc, temp_out]:
            if arcpy.Exists(fc):
                try: arcpy.Delete_management(fc)
                except: pass

        arcpy.AddMessage("SELESAI! Seluruh TK beserta perhitungan koordinat berhasil dibuat.")
        return

# =========================================================
# TOOL 2: PENAMAAN TK BIASA
# =========================================================
class AutoNameTKBiasa(object):
    def __init__(self):
        self.label = "2. Auto-Generate Nama TK"
        self.description = "Penamaan TK Biasa dengan deteksi Segmen-ke-Pantai dan KDEPUM."
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
            displayName="Field Nama Kabupaten (Batas Desa)",
            name="field_kab",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param_kab.parameterDependencies = [param_batas.name]

        param_kec = arcpy.Parameter(
            displayName="Field Nama Kecamatan (Batas Desa)",
            name="field_kec",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param_kec.parameterDependencies = [param_batas.name]

        param_pantai = arcpy.Parameter(
            displayName="Garis Pantai (Polyline) - Kosongkan jika tidak ada",
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
            displayName="Field Nama TK Simpul (Misal: TK 10-000)",
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

        param_out_nama = arcpy.Parameter(
            displayName="Field Target untuk Nama TK Biasa",
            name="field_out_nama",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param_out_nama.parameterDependencies = [param_biasa.name]

        return [param_batas, param_kdepum, param_kab, param_kec, param_pantai, 
                param_simpul, param_id_simpul, param_biasa, param_out_nama]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        return

    # --- FUNGSI BANTUAN ---
    def get_base(self, name):
        if name is None: return ""
        val = name.encode('utf-8') if sys.version_info[0] < 3 and isinstance(name, unicode) else str(name)
        return val.replace("TK ", "").replace("-000", "").strip()

    def parse_id(self, name):
        base = self.get_base(name)
        match = re.match(r'(\d+)([a-zA-Z]*)', base)
        if match:
            return (int(match.group(1)), match.group(2))
        return (base, "")

    def execute(self, parameters, messages):
        batas_desa = parameters[0].valueAsText
        f_kdepum = parameters[1].valueAsText
        f_kab = parameters[2].valueAsText
        f_kec = parameters[3].valueAsText
        garis_pantai = parameters[4].valueAsText
        tk_simpul = parameters[5].valueAsText
        f_id_simpul = parameters[6].valueAsText
        tk_biasa = parameters[7].valueAsText
        f_out_nama = parameters[8].valueAsText

        arcpy.env.overwriteOutput = True

        # 1. SETTING PROYEKSI UTM ON-THE-FLY
        arcpy.AddMessage("1. Mempersiapkan Proyeksi (Auto UTM)...")
        desc = arcpy.Describe(batas_desa)
        sr = desc.spatialReference
        
        if sr.type == "Geographic":
            ext = desc.extent
            cen_x = (ext.XMin + ext.XMax) / 2.0
            utm_zone = int((cen_x + 180) / 6) + 1
            epsg = 32700 + utm_zone if ext.YMin < 0 else 32600 + utm_zone
            work_sr = arcpy.SpatialReference(epsg)
        else:
            work_sr = sr

        # 2. MEMBACA GEOMETRI
        arcpy.AddMessage("2. Membaca geometri spasial...")
        pantai_geoms = []
        if garis_pantai:
            with arcpy.da.SearchCursor(garis_pantai, ["SHAPE@"], spatial_reference=work_sr) as cur:
                for row in cur:
                    if row[0]: pantai_geoms.append(row[0])

        polys = []
        with arcpy.da.SearchCursor(batas_desa, ["OID@", f_kdepum, f_kab, f_kec, "SHAPE@"], spatial_reference=work_sr) as cur:
            for row in cur:
                if row[4]:
                    polys.append({
                        'oid': row[0],
                        'kdepum': str(row[1]).strip() if row[1] else "",
                        'kab': str(row[2]).strip().upper() if row[2] else "",
                        'kec': str(row[3]).strip().upper() if row[3] else "",
                        'geom': row[4]
                    })

        TOL_TOPOLOGY = 15.0 

        arcpy.AddMessage("3. Memetakan Simpul dan Status Batas...")
        simpul_pts = []
        set_kab, set_kec = set(), set()
        
        with arcpy.da.SearchCursor(tk_simpul, ["OID@", f_id_simpul, "SHAPE@"], spatial_reference=work_sr) as cur:
            for row in cur:
                oid, name, geom = row[0], row[1], row[2]
                if not geom: continue
                
                dist_pantai = float('inf')
                if pantai_geoms:
                    dist_pantai = min([pg.distanceTo(geom) for pg in pantai_geoms])

                intersecting = [p for p in polys if p['geom'].distanceTo(geom) <= TOL_TOPOLOGY]
                kabs = set([p['kab'] for p in intersecting if p['kab']])
                kecs = set([p['kec'] for p in intersecting if p['kec']])

                if len(kabs) > 1: set_kab.add(oid)
                elif len(kecs) > 1: set_kec.add(oid)

                simpul_pts.append({
                    'oid': oid, 'name': name, 'geom': geom, 'dist_pantai': dist_pantai
                })

        biasa_pts = []
        with arcpy.da.SearchCursor(tk_biasa, ["OID@", "SHAPE@"], spatial_reference=work_sr) as cur:
            for row in cur:
                if row[1]: biasa_pts.append({'oid': row[0], 'geom': row[1]})

        arcpy.AddMessage("4. Identifikasi Sentuhan Segmen ke Pantai & Pemberian Nama...")
        hasil_nama = {}
        arcpy.SetProgressor("step", "Memproses Segmen...", 0, len(polys), 1)
        
        for poly in polys:
            boundary = poly['geom'].boundary()
            line_len = boundary.length
            pts_on_bound = []
            
            for s in simpul_pts:
                if boundary.distanceTo(s['geom']) <= 1.0:
                    pts_on_bound.append({
                        'type': 'S', 'oid': s['oid'], 'name': s['name'],
                        'measure': boundary.measureOnLine(s['geom']), 'geom': s['geom'],
                        'dist_pantai': s['dist_pantai']
                    })
                    
            for b in biasa_pts:
                if boundary.distanceTo(b['geom']) <= 1.0:
                    pts_on_bound.append({
                        'type': 'B', 'oid': b['oid'],
                        'measure': boundary.measureOnLine(b['geom']), 'geom': b['geom']
                    })

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

                # --- 1. MENANDAI SEGMEN BERSENTUHAN DENGAN PANTAI ---
                # Membangun garis imajiner dari titik-titik di segmen ini
                arr = arcpy.Array()
                for pt in seg:
                    arr.add(pt['geom'].firstPoint)
                seg_line = arcpy.Polyline(arr, work_sr)

                segmen_sentuh_pantai = False
                if pantai_geoms:
                    # Jika garis segmen berjarak <= 50 meter dari pantai, nyatakan BERSENTUHAN!
                    dist_to_pantai = min([seg_line.distanceTo(pg) for pg in pantai_geoms])
                    if dist_to_pantai <= 50.0: 
                        segmen_sentuh_pantai = True

                # --- 2. EKSTRAKSI 2 KDEPUM ---
                test_pt = biasas[0]['geom']
                intersecting_desa = [p for p in polys if p['geom'].distanceTo(test_pt) <= 0.5]
                if len(intersecting_desa) < 2:
                    intersecting_desa = [p for p in polys if p['geom'].distanceTo(test_pt) <= 2.0]
                
                valid_kdepums = list(set([p['kdepum'] for p in intersecting_desa if p['kdepum']]))
                valid_kdepums.sort()
                
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

                kabs = set([p['kab'] for p in intersecting_desa if p['kab']])
                kecs = set([p['kec'] for p in intersecting_desa if p['kec']])
                
                seg_type = 'DESA'
                if len(kabs) > 1: seg_type = 'KAB'
                elif len(kecs) > 1: seg_type = 'KEC'

                # --- 3. HIERARKI PENENTUAN ARAH ---
                dest, origin = None, None
                
                # Rule 1: Jika Segmen ditandai "Sentuh Pantai"
                if segmen_sentuh_pantai:
                    dist_A = s_A['dist_pantai']
                    dist_B = s_B['dist_pantai']
                    # Arahkan ke titik simpul yang posisinya paling dekat ke laut
                    # Batas toleransi 10m digunakan jika segmennya menyusuri pantai secara paralel
                    if abs(dist_A - dist_B) > 10.0: 
                        if dist_A < dist_B: 
                            dest, origin = s_A, s_B
                        else: 
                            dest, origin = s_B, s_A

                # Rule 2 & 3: Kab / Kec / Default
                if not dest:
                    A_kab, B_kab = s_A['oid'] in set_kab, s_B['oid'] in set_kab
                    if seg_type != 'KAB' and (A_kab or B_kab) and (A_kab != B_kab):
                        if A_kab: dest, origin = s_A, s_B
                        else: dest, origin = s_B, s_A
                    else:
                        A_kec, B_kec = s_A['oid'] in set_kec, s_B['oid'] in set_kec
                        if seg_type not in ('KAB', 'KEC') and (A_kec or B_kec) and (A_kec != B_kec):
                            if A_kec: dest, origin = s_A, s_B
                            else: dest, origin = s_B, s_A
                        else:
                            id_A = self.parse_id(s_A['name'])
                            id_B = self.parse_id(s_B['name'])
                            if id_A < id_B:
                                origin, dest = s_A, s_B
                            else:
                                origin, dest = s_B, s_A

                if origin == s_B:
                    biasas.reverse()

                # --- 4. MERANGKAI NAMA ---
                for i, b in enumerate(biasas):
                    num = i + 1
                    name_str = "{0}-{1:03d}".format(prefix, num)
                    hasil_nama[b['oid']] = name_str

            arcpy.SetProgressorPosition()

        # 5. WRITE HASIL
        arcpy.AddMessage("5. Menuliskan {0} Output ke Tabel...".format(len(hasil_nama)))
        with arcpy.da.UpdateCursor(tk_biasa, ["OID@", f_out_nama]) as cur:
            for row in cur:
                oid = row[0]
                if oid in hasil_nama:
                    row[1] = hasil_nama[oid]
                    cur.updateRow(row)

        arcpy.AddMessage("SELESAI! Seluruh aturan telah berhasil diterapkan.")
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

    # --- FUNGSI GENERATE NAMA YANG DIHARAPKAN ---
    def get_expected_names(self, kdepum_list):
        if not kdepum_list:
            return "TK UNKNOWN", "TK UNKNOWN-000"
            
        kdepum_list.sort() 
        
        # 1. Bentuk Nama Simpul
        simpul_name = "TK " + kdepum_list[0]
        for k in kdepum_list[1:]:
            parts = k.split('.')
            if len(parts) >= 4:
                simpul_name += "-{0}.{1}".format(parts[-2], parts[-1])
            else:
                simpul_name += "-" + k
        simpul_name += "-000"
        
        # 2. Bentuk Prefix TK Biasa
        if len(kdepum_list) == 1:
            biasa_prefix = "TK " + kdepum_list[0]
        else:
            k1 = kdepum_list[0]
            k2 = kdepum_list[1]
            parts2 = k2.split('.')
            if len(parts2) >= 4:
                k2_short = "{0}.{1}".format(parts2[-2], parts2[-1])
            else:
                k2_short = k2
            biasa_prefix = "TK {0}-{1}".format(k1, k2_short)
            
        return biasa_prefix, simpul_name

    def execute(self, parameters, messages):
        batas_fc = parameters[0].valueAsText
        f_kdepum = parameters[1].valueAsText
        tk1_fc = parameters[2].valueAsText
        f_tk1 = parameters[3].valueAsText
        tk2_fc = parameters[4].valueAsText
        f_tk2 = parameters[5].valueAsText if tk2_fc else None

        arcpy.env.overwriteOutput = True

        # 1. SETUP PROYEKSI UTM ON-THE-FLY
        desc_batas = arcpy.Describe(batas_fc)
        sr_batas = desc_batas.spatialReference
        
        if sr_batas.type == "Geographic":
            ext = desc_batas.extent
            cen_x = (ext.XMin + ext.XMax) / 2.0
            utm_zone = int((cen_x + 180) / 6) + 1
            epsg = 32700 + utm_zone if ext.YMin < 0 else 32600 + utm_zone
            work_sr = arcpy.SpatialReference(epsg)
            arcpy.AddMessage("1. Auto UTM Zone {0} diaktifkan.".format(utm_zone))
        else:
            work_sr = sr_batas

        # 2. BACA POLIGON DAN BUAT MASTER LINE
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
                    'kdepum': val_kdepum,
                    'ext': geom.extent
                })
                
                boundary = geom.boundary()
                for part in boundary:
                    parts_array.add(part)
                    
        master_line = arcpy.Polyline(parts_array, work_sr)

        # 3. FUNGSI PEMROSESAN QC (SPASIAL + SEMANTIK + EDIT SESSION)
        def process_tk(fc, name_field, label):
            arcpy.AddMessage("3. Menjalankan QC Spasial pada {0}...".format(label))
            
            # Ekstraksi Workspace untuk Edit Session
            desc_fc = arcpy.Describe(fc)
            workspace = desc_fc.path
            
            # Jika di dalam Feature Dataset, mundur 1 folder ke root Geodatabase
            desc_ws = arcpy.Describe(workspace)
            if desc_ws.dataType == "FeatureDataset":
                workspace = desc_ws.path

            fields = [f.name for f in arcpy.ListFields(fc)]
            if "QC_Status" not in fields:
                arcpy.AddField_management(fc, "QC_Status", "TEXT", field_length=255)

            TOLERANSI_SNAP = 0.05 
            error_count, valid_count = 0, 0

            # --- MEMULAI EDIT SESSION SECARA AMAN ---
            edit = arcpy.da.Editor(workspace)
            # Parameter: (with_undo, multiuser_mode)
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
                        
                        if not geom or name_val == "":
                            status_list.append("GEOMETRI/NAMA KOSONG")
                        else:
                            # --- QC 1: SNAP CEK ---
                            dist = geom.distanceTo(master_line)
                            if dist > TOLERANSI_SNAP:
                                status_list.append("TDK SNAP ({0:.2f}m)".format(dist))

                            # --- QC 2: KDEPUM SPASIAL CEK ---
                            intersecting = []
                            for p in polys:
                                if p['geom'].distanceTo(geom) <= 0.5:
                                    intersecting.append(p)
                                    
                            if len(intersecting) < 2:
                                intersecting = [p for p in polys if p['geom'].distanceTo(geom) <= 2.0]
                                
                            valid_kdepums = list(set([p['kdepum'] for p in intersecting if p['kdepum'] != ""]))
                            
                            expected_biasa_prefix, expected_simpul = self.get_expected_names(valid_kdepums)

                            match = re.match(r'^(.+)-(\d{3})$', name_val)
                            if not match:
                                status_list.append("FORMAT SALAH (Tanpa akhiran -XXX)")
                            else:
                                prefix = match.group(1)
                                suffix = match.group(2)
                                
                                if suffix == "000":
                                    if name_val != expected_simpul:
                                        status_list.append("KDEPUM SALAH (Seharusnya: {0})".format(expected_simpul))
                                else:
                                    if prefix != expected_biasa_prefix:
                                        status_list.append("KDEPUM SALAH (Seharusnya: {0}-XXX)".format(expected_biasa_prefix))

                        if len(status_list) > 0:
                            row[2] = "[ERROR] " + " | ".join(status_list)
                            error_count += 1
                        else:
                            row[2] = "VALID"
                            valid_count += 1
                            
                        cur.updateRow(row)
                
                # Selesai dengan sukses, simpan sesi edit
                edit.stopOperation()
                edit.stopEditing(True)

            except Exception as e:
                # Jika gagal di tengah jalan, batalkan perubahan (Rollback)
                edit.abortOperation()
                edit.stopEditing(False)
                arcpy.AddError("Gagal memproses Edit Session: " + str(e))
                raise e
                    
            arcpy.AddMessage("   -> Selesai: {0} Valid, {1} Error ditemukan.".format(valid_count, error_count))

        # 4. EKSEKUSI
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
        self.label = "4. Urutkan & Ekspor Koordinat (Excel)"
        self.description = "Mengurutkan TK berdasarkan garis batas desa ke Excel."
        self.canRunInBackground = False
    
    def getParameterInfo(self):
        """Define parameter definitions"""
        
        # 0. Batas Desa (Polygon)
        param_batas = arcpy.Parameter(
            displayName="Batas Desa (Polygon)",
            name="batas_desa",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input")
        param_batas.filter.list = ["Polygon"]

        # 1. Field Nama Desa
        param_wadkmd = arcpy.Parameter(
            displayName="Field Nama Desa (WADKMD/NAMOBJ)",
            name="wadkmd_field",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param_wadkmd.parameterDependencies = [param_batas.name]

        # 2. Titik Kartometrik (Point)
        param_tk = arcpy.Parameter(
            displayName="Titik Kartometrik (Point)",
            name="titik_kartometrik",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input")
        param_tk.filter.list = ["Point"]

        # 3. Field Remark/Nama TK
        param_remark = arcpy.Parameter(
            displayName="Field Nama TK (Remark)",
            name="remark_field",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param_remark.parameterDependencies = [param_tk.name]

        # 4. Field Latitude (DMS)
        param_lat = arcpy.Parameter(
            displayName="Field Latitude (Lintang)",
            name="lat_field",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param_lat.parameterDependencies = [param_tk.name]

        # 5. Field Longitude (DMS)
        param_long = arcpy.Parameter(
            displayName="Field Longitude (Bujur)",
            name="long_field",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param_long.parameterDependencies = [param_tk.name]

        # 6. Field X (UTM)
        param_x = arcpy.Parameter(
            displayName="Field X (UTM)",
            name="x_field",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param_x.parameterDependencies = [param_tk.name]

        # 7. Field Y (UTM)
        param_y = arcpy.Parameter(
            displayName="Field Y (UTM)",
            name="y_field",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param_y.parameterDependencies = [param_tk.name]

        # 8. Output Excel
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

    # --- HELPER FUNCTIONS ---
    def is_clockwise(self, polyline):
        """Mendeteksi orientasi ring menggunakan Shoelace formula."""
        area = 0
        part = polyline.getPart(0)
        # Perbaikan syntax dari kode sebelumnya: (len(part) - 1)
        for i in range(len(part) - 1):
            p1 = part.getObject(i)
            p2 = part.getObject(i + 1)
            if p1 and p2:
                # Perbaikan syntax rumus Shoelace: (x2 - x1) * (y2 + y1)
                area += (p2.X - p1.X) * (p2.Y + p1.Y)
        return area > 0

    def safe_str(self, val):
        """Menghindari error jika ada data kosong (Null/None) dan support Python 2.7 / 3.x"""
        if val is None:
            return ""
        if sys.version_info[0] < 3: # Jika Python 2.7 (ArcMap)
            if isinstance(val, unicode):
                return val.encode('utf-8').strip()
        return str(val).strip()

    def format_dms(self, val):
        """Menghilangkan angka 0 berlebih di depan derajat & memaksa detik 3 desimal"""
        val_str = self.safe_str(val)
        
        # Deteksi unicode degree symbol aman untuk Python 2 dan 3
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
        """Fungsi untuk membulatkan nilai UTM menjadi 2 angka di belakang koma"""
        try:
            if val is None or str(val).strip() == '':
                return ""
            return "{:.2f}".format(float(val))
        except ValueError:
            return self.safe_str(val)

    # --- MAIN EXECUTE ---
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

        # --- Output ke File Excel (.xls / .xlsx) ---
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
# TOOL 5: LAPORAN DESKRIPSI
# =========================================================
class ExportLaporanTK(object):
    def __init__(self):
        self.label = "5. Buat Laporan Deskripsi Batas (Excel)"
        self.description = "Ekstrak deskripsi lokasi dan arah mata angin TK ke Excel."
        self.canRunInBackground = False
    def getParameterInfo(self):
        # 0. TK Simpul / Gabungan
        param_simpul = arcpy.Parameter(displayName="Titik Kartometrik 1 (Simpul / Gabungan)", name="in_tk_simpul", datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        param_simpul.filter.list = ["Point"]

        # 1. TK Biasa (Opsional)
        param_biasa = arcpy.Parameter(displayName="Titik Kartometrik 2 (TK Biasa - Opsional)", name="in_tk_biasa", datatype="GPFeatureLayer", parameterType="Optional", direction="Input")
        param_biasa.filter.list = ["Point"]

        # 2. Batas Desa (Otomatis diekstrak jadi segmen)
        param_desa = arcpy.Parameter(displayName="Batas Desa (Polygon)", name="in_desa", datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        param_desa.filter.list = ["Polygon"]

        # 3. Penggunaan Lahan (PL)
        param_pl = arcpy.Parameter(displayName="Penggunaan Lahan / PL (Polygon - Opsional)", name="in_pl", datatype="GPFeatureLayer", parameterType="Optional", direction="Input")
        param_pl.filter.list = ["Polygon"]

        # 4. Field PL
        param_f_pl = arcpy.Parameter(displayName="Field Keterangan PL (Contoh: REMARK / JNSPL)", name="field_pl", datatype="Field", parameterType="Optional", direction="Input")
        param_f_pl.parameterDependencies = [param_pl.name]

        # 5. Jalan
        param_jalan = arcpy.Parameter(displayName="Jaringan Jalan (Polyline - Opsional)", name="in_jalan", datatype="GPFeatureLayer", parameterType="Optional", direction="Input")
        param_jalan.filter.list = ["Polyline"]

        # 6. Field Jalan
        param_f_jalan = arcpy.Parameter(displayName="Field Keterangan Jalan (Contoh: NAMOBJ)", name="field_jalan", datatype="Field", parameterType="Optional", direction="Input")
        param_f_jalan.parameterDependencies = [param_jalan.name]

        # 7. Sungai
        param_sungai = arcpy.Parameter(displayName="Jaringan Sungai (Polyline/Polygon - Opsional)", name="in_sungai", datatype="GPFeatureLayer", parameterType="Optional", direction="Input")
        
        # 8. Field Sungai
        param_f_sungai = arcpy.Parameter(displayName="Field Nama Sungai (Contoh: NAMOBJ)", name="field_sungai", datatype="Field", parameterType="Optional", direction="Input")
        param_f_sungai.parameterDependencies = [param_sungai.name]

        # 9. Pantai
        param_pantai = arcpy.Parameter(displayName="Garis Pantai (Polyline - Opsional)", name="in_pantai", datatype="GPFeatureLayer", parameterType="Optional", direction="Input")
        param_pantai.filter.list = ["Polyline"]

        # 10. Output
        param_out = arcpy.Parameter(displayName="Output File Excel", name="out_excel", datatype="DEFile", parameterType="Required", direction="Output")
        param_out.filter.list = ["xls", "xlsx"]

        return [param_simpul, param_biasa, param_desa, 
                param_pl, param_f_pl, param_jalan, param_f_jalan, 
                param_sungai, param_f_sungai, param_pantai, param_out]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        parameters[4].enabled = True if parameters[3].value else False
        parameters[6].enabled = True if parameters[5].value else False
        parameters[8].enabled = True if parameters[7].value else False
        return

    def updateMessages(self, parameters):
        if parameters[3].value and not parameters[4].value: parameters[4].setErrorMessage("Pilih kolom atribut untuk PL.")
        if parameters[5].value and not parameters[6].value: parameters[6].setErrorMessage("Pilih kolom atribut untuk Jalan.")
        if parameters[7].value and not parameters[8].value: parameters[8].setErrorMessage("Pilih kolom atribut untuk Sungai.")
        return

    # --- FUNGSI BANTUAN ---
    def get_arah_mata_angin(self, p1, p2):
        if not p1 or not p2: return "-"
        dx = p2.X - p1.X
        dy = p2.Y - p1.Y
        degrees = math.degrees(math.atan2(dx, dy))
        if degrees < 0: degrees += 360

        if 337.5 <= degrees or degrees < 22.5: return "Utara"
        elif 22.5 <= degrees < 67.5: return "Timur Laut"
        elif 67.5 <= degrees < 112.5: return "Timur"
        elif 112.5 <= degrees < 157.5: return "Tenggara"
        elif 157.5 <= degrees < 202.5: return "Selatan"
        elif 202.5 <= degrees < 247.5: return "Barat Daya"
        elif 247.5 <= degrees < 292.5: return "Barat"
        elif 292.5 <= degrees < 337.5: return "Barat Laut"
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
        try:
            s = unicode(val).strip() if 'unicode' in __builtins__ else str(val).strip()
        except:
            s = str(val).strip()
        return s if s and s.lower() != 'nan' else "-"

    def format_kapital_id(self, text):
        if not text or text == "-": return "-"
        kata_kecil = ["di", "ke", "dari", "dan", "atau", "yang", "untuk", "dengan", "pada"]
        words = str(text).lower().split()
        res = []
        for i, w in enumerate(words):
            if i > 0 and w in kata_kecil: res.append(w)
            else: res.append(w.capitalize())
        return " ".join(res)

    def get_intersect_data(self, tk_buff, target_fc, target_fields, scratch):
        if not target_fc or not target_fields or not target_fields[0]: return {}
        sj_fc = os.path.join(scratch, "sj_temp")
        if arcpy.Exists(sj_fc): arcpy.Delete_management(sj_fc)
        
        arcpy.SpatialJoin_analysis(tk_buff, target_fc, sj_fc, "JOIN_ONE_TO_MANY", "KEEP_ALL", "", "INTERSECT")
        res = {}
        with arcpy.da.SearchCursor(sj_fc, ['TK_ID'] + target_fields) as sc:
            for row in sc:
                if row[1] is not None:
                    res.setdefault(row[0], []).append(row[1:])
        arcpy.Delete_management(sj_fc)
        return res

    # --- EKSEKUSI UTAMA ---
    def execute(self, parameters, messages):
        try:
            in_tk_simpul = parameters[0].valueAsText
            in_tk_biasa  = parameters[1].valueAsText
            in_desa      = parameters[2].valueAsText
            
            in_pl        = parameters[3].valueAsText
            f_pl_nama    = parameters[4].valueAsText
            
            in_jalan     = parameters[5].valueAsText
            f_jln_nama   = parameters[6].valueAsText
            
            in_sungai    = parameters[7].valueAsText
            f_sng_nama   = parameters[8].valueAsText
            
            in_pantai    = parameters[9].valueAsText
            out_excel    = parameters[10].valueAsText

            if not out_excel.lower().endswith(('.xls', '.xlsx')):
                out_excel += '.xls'

            arcpy.env.overwriteOutput = True
            scratch = arcpy.env.scratchGDB
            
            # Setup Proyeksi & Gabung TK
            sr = arcpy.Describe(in_tk_simpul).spatialReference
            arcpy.env.outputCoordinateSystem = sr
            tk_merged = os.path.join(scratch, "tk_merged")

            if in_tk_biasa:
                arcpy.AddMessage("1. Menggabungkan layer TK Simpul dan TK Biasa...")
                arcpy.Merge_management([in_tk_simpul, in_tk_biasa], tk_merged)
            else:
                arcpy.AddMessage("1. Menggunakan layer TK Simpul (termasuk TK Biasa di dalamnya)...")
                arcpy.CopyFeatures_management(in_tk_simpul, tk_merged)

            arcpy.AddMessage("2. Membaca Atribut dan Membuat Garis Segmen dari Poligon...")
            f_tk_nama = self.get_field_name(tk_merged, ["NAMOBJ", "Nama_TK", "Nama"]) or "NAMOBJ"
            f_tk_lat = self.get_field_name(tk_merged, ["KOORDY_DMS", "LINTANG", "Lat"])
            f_tk_lon = self.get_field_name(tk_merged, ["KOORDX_DMS", "BUJUR", "Long"])
            f_tk_x = self.get_field_name(tk_merged, ["UTMX", "X"])
            f_tk_y = self.get_field_name(tk_merged, ["UTMY", "Y"])

            f_desa_kd = self.get_field_name(in_desa, ["KDEPUM"]) or "KDEPUM"
            f_desa_nama = self.get_field_name(in_desa, ["NAMOBJ", "Desa"]) or "NAMOBJ"
            f_desa_kec = self.get_field_name(in_desa, ["WADMKC", "Kecamatan"]) or "WADMKC"

            # --- EKSTRAKSI GARIS SEGMEN OTOMATIS ---
            temp_segmen = os.path.join(scratch, "temp_segmen_desa")
            arcpy.FeatureToLine_management(in_desa, temp_segmen, "", "NO_ATTRIBUTES")
            
            # Membaca informasi poligon desa ke memori
            desa_polys = []
            with arcpy.da.SearchCursor(in_desa, ["OID@", f_desa_nama, "SHAPE@"]) as cur:
                for row in cur:
                    desa_polys.append({
                        'oid': row[0],
                        'nama': self.format_kapital_id(self.val_str(row[1])),
                        'geom': row[2]
                    })
            
            # Memberi nama segmen berdasarkan irisan desa di kiri/kanannya
            segmen_geom = {}
            with arcpy.da.SearchCursor(temp_segmen, ["OID@", "SHAPE@"]) as cur:
                for row in cur:
                    s_oid = row[0]
                    geom = row[1]
                    if not geom: continue
                    
                    # Titik tengah garis
                    mid_pt = geom.positionAlongLine(0.5, True).firstPoint
                    
                    # Deteksi Desa (Toleransi 1 Meter)
                    desa_terhubung = []
                    for p in desa_polys:
                        if p['geom'].distanceTo(mid_pt) <= 1.0:
                            if p['nama'] and p['nama'] != "-":
                                desa_terhubung.append(p['nama'])
                                
                    desa_terhubung = sorted(list(set(desa_terhubung)))
                    
                    if len(desa_terhubung) >= 2:
                        s_nama = "Batas Desa {0} - Desa {1}".format(desa_terhubung[0], desa_terhubung[1])
                    elif len(desa_terhubung) == 1:
                        s_nama = "Batas Desa {0} (Luar)".format(desa_terhubung[0])
                    else:
                        s_nama = "Batas Administratif"
                        
                    segmen_geom[s_oid] = {'nama': s_nama, 'geom': geom}

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
                        'lat': self.val_str(row[3]) if f_tk_lat else "-", 'lon': self.val_str(row[4]) if f_tk_lon else "-",
                        'x': self.val_str(row[5]) if f_tk_x else "-", 'y': self.val_str(row[6]) if f_tk_y else "-"
                    }

            sj_segmen = os.path.join(scratch, "sj_segmen")
            # Hubungkan TK dengan Garis Segmen Otomatis
            arcpy.SpatialJoin_analysis(tk_pts, temp_segmen, sj_segmen, "JOIN_ONE_TO_MANY", "KEEP_ALL", "", "WITHIN_A_DISTANCE", "1 Meters")
            
            segmen_dict = {} 
            with arcpy.da.SearchCursor(sj_segmen, ['TARGET_FID', 'JOIN_FID']) as sc:
                for row in sc:
                    tk_id, seg_id = row[0], row[1]
                    if seg_id != -1: segmen_dict.setdefault(seg_id, []).append(tk_id)

            tk_buff = os.path.join(scratch, "tk_buff_temp")
            arcpy.Buffer_analysis(tk_pts, tk_buff, "3 Meters") 

            arcpy.AddMessage("   -> Membaca relasi dengan Lahan, Jalan, dan Sungai...")
            tk_desa_rel = self.get_intersect_data(tk_buff, in_desa, [f_desa_kd, f_desa_nama, f_desa_kec], scratch)
            tk_pl = self.get_intersect_data(tk_buff, in_pl, [f_pl_nama], scratch) if in_pl else {}
            tk_jln = self.get_intersect_data(tk_buff, in_jalan, [f_jln_nama], scratch) if in_jalan else {}
            tk_sng = self.get_intersect_data(tk_buff, in_sungai, [f_sng_nama], scratch) if in_sungai else {}
            tk_pnt = self.get_intersect_data(tk_buff, in_pantai, ['OID@'], scratch) if in_pantai else {}

            arcpy.AddMessage("4. Menyusun Deskripsi dengan Ejaan Bahasa Indonesia...")
            out_table = os.path.join(scratch, "Tabel_Output_TK")
            if arcpy.Exists(out_table): arcpy.Delete_management(out_table)
            arcpy.CreateTable_management(scratch, "Tabel_Output_TK")
            
            fields_def = [
                ("No_Urut", "LONG"), ("Segmen", "TEXT", 255), ("Titik_Kart", "TEXT", 255),
                ("Lintang", "TEXT", 50), ("Bujur", "TEXT", 50), ("X", "TEXT", 50), ("Y", "TEXT", 50),
                ("Deskripsi_titik", "TEXT", 1000), ("Deskripsi", "TEXT", 1000), ("Arah_Mata_Angin", "TEXT", 50)
            ]
            for fd in fields_def:
                arcpy.AddField_management(out_table, fd[0], fd[1], field_length=fd[2] if len(fd)>2 else None)

            ins_fields = [fd[0] for fd in fields_def]
            no_urut_global = 1

            with arcpy.da.InsertCursor(out_table, ins_fields) as ic:
                for seg_id, tk_ids in segmen_dict.items():
                    if seg_id not in segmen_geom: continue
                    
                    s_geom = segmen_geom[seg_id]['geom']
                    s_nama = segmen_geom[seg_id]['nama']
                    
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
                        tid = item[1]
                        pt_info = item[2]
                        raw_pt = item[3]
                        
                        nama_titik = pt_info['nama']
                        
                        raw_pl = tk_pl[tid][0][0] if tid in tk_pl else "-"
                        raw_jln = tk_jln[tid][0][0] if tid in tk_jln else "-"
                        raw_sng = tk_sng[tid][0][0] if tid in tk_sng else ""
                        
                        jnspl_val = self.format_kapital_id(raw_pl)
                        jalan_val = self.format_kapital_id(raw_jln)
                        sungai_val = self.format_kapital_id(raw_sng)
                        is_pantai = tid in tk_pnt
                        
                        if not sungai_val or sungai_val.lower() == "nan": sungai_val = ""

                        deskripsi_tambahan = ""
                        if sungai_val:
                            if sungai_val.lower().startswith("sungai"): deskripsi_tambahan = "berada di {}".format(sungai_val)
                            else: deskripsi_tambahan = "berada di Sungai {}".format(sungai_val)
                        else:
                            deskripsi_tambahan = "melewati {}".format(jnspl_val)

                        deskripsi_titik = ""
                        if nama_titik.endswith("-000"):
                            teks_sungai_simpul = ""
                            if sungai_val:
                                if sungai_val.lower().startswith("sungai"): teks_sungai_simpul = " di {}".format(sungai_val)
                                else: teks_sungai_simpul = " di Sungai {}".format(sungai_val)
                            
                            desa_list = tk_desa_rel.get(tid, [])
                            if desa_list:
                                desa_list.sort(key=lambda x: str(x[0]))
                                kec_dict = {}
                                for d in desa_list:
                                    kec = self.format_kapital_id(self.val_str(d[2]))
                                    kec_dict.setdefault(kec, []).append("Desa " + self.format_kapital_id(self.val_str(d[1])))
                                
                                kec_groups = []
                                for kec, desas in kec_dict.items():
                                    kec_groups.append("{0} Kecamatan {1}".format(", ".join(desas), kec))
                                    
                                teks_wilayah = " dan ".join(kec_groups)
                                base_teks_desa = "titik simpul dari {0}{1}".format(teks_wilayah, teks_sungai_simpul)
                            else:
                                base_teks_desa = "titik simpul{0} (Tidak ada data desa)".format(teks_sungai_simpul)
                                
                            if is_pantai: teks_tambahan = " yang terletak di garis pantai merujuk pada data Garis Pantai Peta Rupa Bumi Indonesia Skala 1:5.000, Tahun 2025"
                            else: teks_tambahan = " yang terletak di {}".format(jnspl_val)
                                
                            deskripsi_titik = base_teks_desa + teks_tambahan
                        else:
                            if sungai_val:
                                if sungai_val.lower() == "tanpa nama": deskripsi_titik = "Perairan"
                                elif sungai_val.lower().startswith("sungai"): deskripsi_titik = sungai_val
                                else: deskripsi_titik = "Sungai {}".format(sungai_val)
                            elif jalan_val != "-":
                                deskripsi_titik = jalan_val
                            else:
                                deskripsi_titik = jnspl_val

                        arah = "-"
                        if i < len(pts_list) - 1:
                            arah = self.get_arah_mata_angin(raw_pt, pts_list[i+1][3])

                        row_vals = (
                            no_urut_global, s_nama, nama_titik,
                            pt_info['lat'], pt_info['lon'], pt_info['x'], pt_info['y'],
                            deskripsi_titik, deskripsi_tambahan, arah
                        )
                        ic.insertRow(row_vals)
                        no_urut_global += 1

            arcpy.AddMessage("5. Mengekspor Data ke Excel...")
            arcpy.TableToExcel_conversion(out_table, out_excel)
            
            for temp_fc in [tk_merged, tk_pts, tk_buff, out_table, temp_segmen, sj_segmen]:
                if arcpy.Exists(temp_fc):
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