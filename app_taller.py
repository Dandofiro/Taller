import os
import hmac
import sqlite3
from datetime import datetime

import pandas as pd
import streamlit as st
from fpdf import FPDF

try:
    import psycopg2
except ImportError:
    HAS_PSYCOPG2 = False


# ==========================================
# CONFIGURACIÓN DEL NEGOCIO (FLORIDA)
# ==========================================
NOMBRE_TALLER = "TU TALLER MECANICO LLC"
DIRECCION_TALLER = "Miami / Orlando, FL"
TELEFONO_TALLER = "(305) 555-0199"
REGISTRO_FDACS = "MV-99999 (En trámite / Colocar tu # FDACS)"
TAX_RATE_FL = 0.07  # 6% Estatal FL + 1% Discrecional del Condado (ajustar según tu condado)
DB_FILE = "taller_florida.db"

st.set_page_config(page_title="Taller FL - Gestión", page_icon="🔧", layout="centered")

# ==========================================
# AUTENTICACIÓN CON CONTRASEÑA
# ==========================================
def get_app_password() -> str:
    try:
        if "APP_PASSWORD" in st.secrets:
            return str(st.secrets["APP_PASSWORD"])
    except Exception:
        pass
    return os.getenv("APP_PASSWORD", "TallerFL2026!")

def check_login() -> bool:
    if st.session_state.get("authenticated", False):
        return True

    st.title("🔐 Acceso - Taller Móvil FL")
    st.caption("Ingresa tu contraseña para gestionar agenda, órdenes y contabilidad.")

    with st.form("login_form"):
        pwd = st.text_input("Contraseña", type="password")
        submitted = st.form_submit_button("Entrar", use_container_width=True)

        if submitted:
            expected = get_app_password()
            if hmac.compare_digest(pwd, expected):
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Contraseña incorrecta.")

    return False

if not check_login():
    st.stop()

# ==========================================
# CONEXIÓN A BASE DE DATOS (POSTGRESQL / SQLITE)
# ==========================================
def get_database_url() -> str:
    try:
        if "DATABASE_URL" in st.secrets:
            return str(st.secrets["DATABASE_URL"]).strip()
    except Exception:
        pass
    return os.getenv("DATABASE_URL", "").strip()

def is_postgres() -> bool:
    return bool(get_database_url())

def get_conn():
    db_url = get_database_url()
    if db_url:
        try:
            import psycopg2
        except ImportError as exc:
            raise RuntimeError("Falta instalar 'psycopg2-binary' para conectar a PostgreSQL.") from exc
        return psycopg2.connect(db_url)
    return sqlite3.connect(DB_FILE, check_same_thread=False)

def ph() -> str:
    return "%s" if is_postgres() else "?"

def read_df(query: str, params=None) -> pd.DataFrame:
    conn = get_conn()
    try:
        if is_postgres():
            q = query.replace("?", "%s")
            with conn.cursor() as cur:
                cur.execute(q, params or ())
                cols = [desc[0] for desc in cur.description] if cur.description else []
                rows = cur.fetchall()
            return pd.DataFrame(rows, columns=cols)
        return pd.read_sql_query(query, conn, params=params or ())
    finally:
        conn.close()

def execute_write(query: str, params=None, return_id: bool = False):
    conn = get_conn()
    try:
        q = query.replace("?", "%s") if is_postgres() else query
        if is_postgres():
            with conn.cursor() as cur:
                stmt = f"{q.rstrip().rstrip(';')} RETURNING id" if return_id else q
                cur.execute(stmt, params or ())
                last_id = cur.fetchone()[0] if return_id else None
            conn.commit()
            return last_id
        else:
            cur = conn.cursor()
            cur.execute(q, params or ())
            last_id = cur.lastrowid if return_id else None
            conn.commit()
            return last_id
    finally:
        conn.close()

def init_db():
    if is_postgres():
        pk = "SERIAL PRIMARY KEY"
        real_t = "DOUBLE PRECISION"
    else:
        pk = "INTEGER PRIMARY KEY AUTOINCREMENT"
        real_t = "REAL"

    ddl_statements = [
        f"""CREATE TABLE IF NOT EXISTS citas (
            id {pk},
            fecha TEXT NOT NULL,
            hora TEXT NOT NULL,
            cliente_nombre TEXT NOT NULL,
            telefono TEXT NOT NULL,
            vehiculo TEXT NOT NULL,
            direccion TEXT,
            motivo TEXT,
            estado TEXT DEFAULT 'Pendiente'
        )""",
        f"""CREATE TABLE IF NOT EXISTS clientes (
            id {pk},
            nombre TEXT NOT NULL,
            telefono TEXT NOT NULL,
            email TEXT
        )""",
        f"""CREATE TABLE IF NOT EXISTS vehiculos (
            id {pk},
            cliente_id INTEGER REFERENCES clientes(id),
            marca_modelo_anio TEXT NOT NULL,
            placa TEXT,
            vin TEXT
        )""",
        f"""CREATE TABLE IF NOT EXISTS ordenes (
            id {pk},
            vehiculo_id INTEGER REFERENCES vehiculos(id),
            fecha_ingreso TEXT,
            fecha_prometida TEXT,
            odometro_entrada INTEGER,
            odometro_salida INTEGER,
            estado TEXT,
            monto_presupuestado {real_t} DEFAULT 0.0,
            aprobacion_extra INTEGER DEFAULT 0,
            devolucion_piezas INTEGER DEFAULT 0,
            notas TEXT
        )""",
        f"""CREATE TABLE IF NOT EXISTS items_orden (
            id {pk},
            orden_id INTEGER REFERENCES ordenes(id),
            tipo TEXT,
            condicion TEXT,
            descripcion TEXT,
            cantidad {real_t},
            precio_unitario {real_t},
            tasa_ambiental {real_t} DEFAULT 0.0
        )""",
        f"""CREATE TABLE IF NOT EXISTS finanzas (
            id {pk},
            fecha TEXT,
            tipo TEXT,
            categoria TEXT,
            descripcion TEXT,
            orden_id INTEGER,
            subtotal {real_t},
            sales_tax {real_t},
            total {real_t},
            metodo_pago TEXT
        )"""
    ]

    conn = get_conn()
    try:
        if is_postgres():
            with conn.cursor() as cur:
                for stmt in ddl_statements:
                    cur.execute(stmt)
        else:
            cur = conn.cursor()
            for stmt in ddl_statements:
                cur.execute(stmt)
        conn.commit()
    finally:
        conn.close()

init_db()

# ==========================================
# LÓGICA FISCAL DE FLORIDA (SALES TAX)
# ==========================================
def calcular_totales_florida(df_items: pd.DataFrame):
    """
    Regla FDOR Florida:
    - Si la reparación incluye repuestos/materiales, el Sales Tax aplica sobre
      el TOTAL (Mano de obra + Repuestos + tasas ambientales).
    - Si es 100% mano de obra sin repuestos, el Sales Tax es $0.00.
    - Las tasas ambientales de baterías ($1.50) y neumáticos ($1.00) se suman aparte.
    """
    if df_items.empty:
        return 0.0, 0.0, 0.0, 0.0, 0.0

    df_calc = df_items.copy()
    df_calc["total_linea"] = df_calc["cantidad"] * df_calc["precio_unitario"]
    subtotal_mo = float(df_calc[df_calc["tipo"] == "MANO_OBRA"]["total_linea"].sum())
    subtotal_rep = float(df_calc[df_calc["tipo"] == "REPUESTO"]["total_linea"].sum())
    tasas_amb = float((df_calc["cantidad"] * df_calc["tasa_ambiental"]).sum())

    subtotal_general = subtotal_mo + subtotal_rep
    tiene_repuestos = subtotal_rep > 0

    if tiene_repuestos:
        sales_tax = round((subtotal_general + tasas_amb) * TAX_RATE_FL, 2)
    else:
        sales_tax = 0.0

    total_final = round(subtotal_general + tasas_amb + sales_tax, 2)
    return subtotal_mo, subtotal_rep, tasas_amb, sales_tax, total_final

# ==========================================
# GENERADOR DE PDF LEGAL PARA FLORIDA
# ==========================================
def safe_pdf_text(text) -> str:
    # FPDF clásico usa latin-1; reemplaza caracteres no soportados para evitar errores
    return str(text or "").encode("latin-1", "replace").decode("latin-1")

def generar_pdf_orden(orden_id: int) -> str:
    p = ph()
    df_orden = read_df(f"""
        SELECT o.*, v.marca_modelo_anio, v.placa, v.vin, c.nombre, c.telefono
        FROM ordenes o
        JOIN vehiculos v ON o.vehiculo_id = v.id
        JOIN clientes c ON v.cliente_id = c.id
        WHERE o.id = {p}
    """, (int(orden_id),))
    orden = df_orden.iloc[0]

    items = read_df(f"SELECT * FROM items_orden WHERE orden_id = {p} ORDER BY id ASC", (int(orden_id),))
    sub_mo, sub_rep, tasas, tax, total = calcular_totales_florida(items)

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 8, safe_pdf_text(NOMBRE_TALLER), ln=True)
    pdf.set_font("Arial", "", 9)
    pdf.cell(0, 5, safe_pdf_text(f"{DIRECCION_TALLER} | Tel: {TELEFONO_TALLER}"), ln=True)
    pdf.set_font("Arial", "B", 9)
    pdf.cell(0, 5, safe_pdf_text(f"FL Motor Vehicle Repair Reg #: {REGISTRO_FDACS}"), ln=True)
    pdf.ln(4)

    pdf.set_font("Arial", "B", 12)
    doc_tipo = "PRESUPUESTO / ESTIMATE" if orden["estado"] == "Presupuesto" else "FACTURA / INVOICE"
    pdf.cell(0, 8, safe_pdf_text(f"{doc_tipo} #00{orden_id} - Estado: {orden['estado']}"), ln=True, border="B")
    pdf.ln(2)

    pdf.set_font("Arial", "", 9)
    pdf.cell(95, 5, safe_pdf_text(f"Cliente: {orden['nombre']}"), ln=False)
    pdf.cell(95, 5, safe_pdf_text(f"Fecha Ingreso: {orden['fecha_ingreso']}"), ln=True)
    pdf.cell(95, 5, safe_pdf_text(f"Telefono: {orden['telefono']}"), ln=False)
    pdf.cell(95, 5, safe_pdf_text(f"Fecha Prometida: {orden['fecha_prometida']}"), ln=True)
    pdf.cell(95, 5, safe_pdf_text(f"Vehiculo: {orden['marca_modelo_anio']}"), ln=False)
    pdf.cell(95, 5, safe_pdf_text(f"Placa / VIN: {orden['placa']} / {orden['vin']}"), ln=True)
    pdf.cell(95, 5, safe_pdf_text(f"Odometro Entrada: {orden['odometro_entrada']} mi"), ln=False)
    pdf.cell(95, 5, safe_pdf_text(f"Odometro Salida: {orden['odometro_salida']} mi"), ln=True)
    pdf.ln(4)

    pdf.set_font("Arial", "B", 9)
    pdf.cell(25, 6, "Tipo", border=1)
    pdf.cell(25, 6, "Condicion", border=1)
    pdf.cell(85, 6, "Descripcion", border=1)
    pdf.cell(15, 6, "Cant.", border=1, align="C")
    pdf.cell(20, 6, "P.Unit", border=1, align="R")
    pdf.cell(20, 6, "Total", border=1, align="R", ln=True)

    pdf.set_font("Arial", "", 8)
    for _, r in items.iterrows():
        tot_l = float(r["cantidad"]) * float(r["precio_unitario"])
        pdf.cell(25, 6, safe_pdf_text(r["tipo"]), border=1)
        pdf.cell(25, 6, safe_pdf_text(r["condicion"]), border=1)
        pdf.cell(85, 6, safe_pdf_text(r["descripcion"])[:45], border=1)
        pdf.cell(15, 6, safe_pdf_text(r["cantidad"]), border=1, align="C")
        pdf.cell(20, 6, f"${float(r['precio_unitario']):.2f}", border=1, align="R")
        pdf.cell(20, 6, f"${tot_l:.2f}", border=1, align="R", ln=True)

    pdf.ln(3)
    pdf.set_font("Arial", "", 9)
    pdf.cell(150, 5, "Subtotal Mano de Obra:", align="R")
    pdf.cell(40, 5, f"${sub_mo:.2f}", align="R", ln=True)
    pdf.cell(150, 5, "Subtotal Repuestos:", align="R")
    pdf.cell(40, 5, f"${sub_rep:.2f}", align="R", ln=True)
    if tasas > 0:
        pdf.cell(150, 5, "FL Environmental Fees (Baterias/Llantas):", align="R")
        pdf.cell(40, 5, f"${tasas:.2f}", align="R", ln=True)
    pdf.cell(150, 5, f"FL Sales Tax ({TAX_RATE_FL*100:.1f}%):", align="R")
    pdf.cell(40, 5, f"${tax:.2f}", align="R", ln=True)
    pdf.set_font("Arial", "B", 11)
    pdf.cell(150, 7, "TOTAL A PAGAR (USD):", align="R")
    pdf.cell(40, 7, f"${total:.2f}", align="R", ln=True)

    pdf.ln(5)
    pdf.set_font("Arial", "B", 7)
    pdf.cell(0, 4, "AVISO LEGAL DEL ESTADO DE FLORIDA (FS 559.905):", ln=True)
    pdf.set_font("Arial", "", 7)
    dev_txt = (
        "SI, el cliente solicita devolucion de piezas reemplazadas."
        if orden["devolucion_piezas"]
        else "NO, el cliente renuncia a conservar piezas reemplazadas."
    )
    pdf.multi_cell(0, 3.5, safe_pdf_text(
        f"1. Piezas viejas: {dev_txt}\n"
        "2. Bajo la ley de Florida, el cargo final no excedera el presupuesto escrito en mas del 10% sin autorizacion previa del cliente.\n"
        "3. Garantia: 90 dias o 3,000 millas en mano de obra. Repuestos sujetos a garantia del fabricante."
    ))

    out_dir = "/tmp" if os.path.exists("/tmp") else "."
    safe_name = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in str(orden["nombre"]))
    filename = os.path.join(out_dir, f"Orden_{orden_id}_{safe_name}.pdf")
    pdf.output(filename)
    return filename

# ==========================================
# ENCABEZADO Y CIERRE DE SESIÓN
# ==========================================
col_t1, col_t2 = st.columns([4, 1])
with col_t1:
    st.title("🔧 Taller Móvil FL")
    st.caption("Base de datos: PostgreSQL" if is_postgres() else "Base de datos: SQLite local")
with col_t2:
    st.write("")
    if st.button("Salir"):
        st.session_state["authenticated"] = False
        st.rerun()

menu = st.tabs(["📅 Agenda", "📋 Nueva Orden", "🛠️ Órdenes", "💵 Contabilidad"])

# ------------------------------------------
# TAB 0: AGENDA DE CITAS Y RUTAS (MÓVIL)
# ------------------------------------------
with menu[0]:
    st.subheader("Agenda de Servicios")

    with st.expander("➕ Agendar Nueva Cita", expanded=False):
        with st.form("form_nueva_cita", clear_on_submit=True):
            col_f1, col_f2 = st.columns(2)
            with col_f1:
                cita_fecha = st.date_input("Fecha de visita")
            with col_f2:
                cita_hora = st.time_input("Hora estimada")

            cita_cli = st.text_input("Nombre del Cliente *")
            cita_tel = st.text_input("Teléfono (WhatsApp/SMS) *")
            cita_veh = st.text_input("Vehículo (Año, Marca, Modelo) *", placeholder="Ej: 2020 Toyota Camry")
            cita_dir = st.text_input("Dirección / Ubicación del vehículo", placeholder="Ej: 1234 NW 7th St, Miami, FL")
            cita_mot = st.text_area("Trabajo / Falla reportada")

            if st.form_submit_button("Guardar Cita", use_container_width=True):
                if cita_cli.strip() and cita_tel.strip() and cita_veh.strip():
                    p = ph()
                    execute_write(
                        f"""INSERT INTO citas (fecha, hora, cliente_nombre, telefono, vehiculo, direccion, motivo, estado)
                            VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, 'Pendiente')""",
                        (
                            str(cita_fecha),
                            cita_hora.strftime("%H:%M"),
                            cita_cli.strip(),
                            cita_tel.strip(),
                            cita_veh.strip(),
                            cita_dir.strip(),
                            cita_mot.strip(),
                        ),
                    )
                    st.success("✅ Cita agendada.")
                    st.rerun()
                else:
                    st.error("Completa cliente, teléfono y vehículo (*).")

    ver_todas = st.checkbox("Mostrar citas completadas / canceladas", value=False)
    query_citas = (
        "SELECT * FROM citas ORDER BY fecha ASC, hora ASC"
        if ver_todas
        else "SELECT * FROM citas WHERE estado IN ('Pendiente', 'En Camino') ORDER BY fecha ASC, hora ASC"
    )
    df_citas = read_df(query_citas)

    if df_citas.empty:
        st.info("No tienes citas pendientes en la agenda.")
    else:
        for _, c in df_citas.iterrows():
            cid = int(c["id"])
            with st.container(border=True):
                st.markdown(f"**🕒 {c['fecha']} — {c['hora']}** | `{c['estado']}`")
                st.markdown(f"**{c['cliente_nombre']}** ({c['telefono']}) — *{c['vehiculo']}*")
                if c["direccion"]:
                    st.caption(f"📍 {c['direccion']}")
                if c["motivo"]:
                    st.write(c["motivo"])

                col_b1, col_b2, col_b3 = st.columns(3)
                with col_b1:
                    if c["direccion"]:
                        maps_q = str(c["direccion"]).replace(" ", "+")
                        #st.link_button("🗺️ Mapa", f"https://maps.apple.com/?q={maps_q}", use_container_width=True)
                        st.link_button("🗺️ Mapa", f"https://www.google.com/maps/search/?api=1&query={maps_q}", use_container_width=True)
                with col_b2:
                    if c["estado"] in ("Pendiente", "En Camino"):
                        if st.button("🔧 Iniciar Orden", key=f"conv_{cid}", use_container_width=True):
                            p = ph()
                            cli_id = execute_write(
                                f"INSERT INTO clientes (nombre, telefono) VALUES ({p}, {p})",
                                (c["cliente_nombre"], c["telefono"]),
                                return_id=True,
                            )
                            veh_id = execute_write(
                                f"INSERT INTO vehiculos (cliente_id, marca_modelo_anio, placa, vin) VALUES ({p}, {p}, '', '')",
                                (cli_id, c["vehiculo"]),
                                return_id=True,
                            )
                            execute_write(
                                f"""INSERT INTO ordenes
                                    (vehiculo_id, fecha_ingreso, fecha_prometida, odometro_entrada, odometro_salida, estado, devolucion_piezas, notas)
                                    VALUES ({p}, {p}, {p}, 0, 0, 'Presupuesto', 0, {p})""",
                                (
                                    veh_id,
                                    datetime.now().strftime("%Y-%m-%d"),
                                    c["fecha"],
                                    c["motivo"] or "",
                                ),
                            )
                            execute_write(f"UPDATE citas SET estado = 'Convertida a Orden' WHERE id = {p}", (cid,))
                            st.success("Orden creada. Ve a la pestaña 'Órdenes' para completar odómetro, repuestos y mano de obra.")
                            st.rerun()
                with col_b3:
                    if c["estado"] in ("Pendiente", "En Camino"):
                        if st.button("❌ Cancelar", key=f"canc_{cid}", use_container_width=True):
                            p = ph()
                            execute_write(f"UPDATE citas SET estado = 'Cancelada' WHERE id = {p}", (cid,))
                            st.rerun()

# ------------------------------------------
# TAB 1: CREAR CLIENTE, VEHÍCULO Y ORDEN
# ------------------------------------------
with menu[1]:
    st.subheader("Registrar Servicio / Presupuesto")
    with st.form("form_nueva_orden", clear_on_submit=True):
        c_nombre = st.text_input("Nombre del Cliente *")
        c_tel = st.text_input("Teléfono (WhatsApp/SMS) *")
        v_desc = st.text_input("Vehículo (Año, Marca, Modelo) *", placeholder="Ej: 2018 Ford F-150")
        col1, col2 = st.columns(2)
        with col1:
            v_placa = st.text_input("Placa")
            odo_in = st.number_input("Odómetro Entrada (mi) *", min_value=0, step=100)
        with col2:
            v_vin = st.text_input("VIN (Opcional)")
            f_prom = st.date_input("Fecha Prometida")

        dev_piezas = st.checkbox("Cliente solicita devolución de piezas viejas (Ley FL)")
        notas = st.text_area("Falla reportada por el cliente")

        if st.form_submit_button("Crear Orden de Servicio", use_container_width=True):
            if c_nombre.strip() and c_tel.strip() and v_desc.strip():
                p = ph()
                cli_id = execute_write(
                    f"INSERT INTO clientes (nombre, telefono) VALUES ({p}, {p})",
                    (c_nombre.strip(), c_tel.strip()),
                    return_id=True,
                )
                veh_id = execute_write(
                    f"INSERT INTO vehiculos (cliente_id, marca_modelo_anio, placa, vin) VALUES ({p}, {p}, {p}, {p})",
                    (cli_id, v_desc.strip(), v_placa.strip(), v_vin.strip()),
                    return_id=True,
                )
                execute_write(
                    f"""INSERT INTO ordenes
                        (vehiculo_id, fecha_ingreso, fecha_prometida, odometro_entrada, odometro_salida, estado, devolucion_piezas, notas)
                        VALUES ({p}, {p}, {p}, {p}, {p}, 'Presupuesto', {p}, {p})""",
                    (
                        veh_id,
                        datetime.now().strftime("%Y-%m-%d"),
                        str(f_prom),
                        int(odo_in),
                        int(odo_in),
                        int(dev_piezas),
                        notas.strip(),
                    ),
                )
                st.success("✅ Orden creada. Ve a 'Órdenes' para agregar repuestos y mano de obra.")
            else:
                st.error("Por favor completa los campos obligatorios (*).")

# ------------------------------------------
# TAB 2: GESTIONAR ÍTEMS, REGLA 10% Y PDF
# ------------------------------------------
with menu[2]:
    df_ordenes = read_df("""
        SELECT o.id, c.nombre, v.marca_modelo_anio, o.estado, o.monto_presupuestado, o.aprobacion_extra, o.odometro_entrada, o.odometro_salida
        FROM ordenes o
        JOIN vehiculos v ON o.vehiculo_id = v.id
        JOIN clientes c ON v.cliente_id = c.id
        ORDER BY o.id DESC
    """)

    if df_ordenes.empty:
        st.info("No hay órdenes registradas aún.")
    else:
        opciones = {
            f"#{r['id']} - {r['nombre']} ({r['marca_modelo_anio']}) [{r['estado']}]": int(r["id"])
            for _, r in df_ordenes.iterrows()
        }
        sel_label = st.selectbox("Selecciona una Orden:", list(opciones.keys()))
        oid = opciones[sel_label]
        orden_actual = df_ordenes[df_ordenes["id"] == oid].iloc[0]

        # Actualizar odómetro de entrada si la orden vino desde la Agenda en 0
        if int(orden_actual["odometro_entrada"] or 0) == 0:
            with st.expander("📏 Registrar Odómetro de Entrada (Obligatorio FL)", expanded=True):
                odo_in_edit = st.number_input("Millaje actual del vehículo", min_value=0, step=100, key=f"odoin_{oid}")
                if st.button("Guardar Odómetro Entrada", key=f"save_odoin_{oid}"):
                    p = ph()
                    execute_write(
                        f"UPDATE ordenes SET odometro_entrada = {p}, odometro_salida = {p} WHERE id = {p}",
                        (int(odo_in_edit), int(odo_in_edit), oid),
                    )
                    st.rerun()

        with st.expander("➕ Agregar Mano de Obra o Repuesto", expanded=True):
            with st.form("add_item", clear_on_submit=True):
                i_tipo = st.selectbox("Tipo", ["MANO_OBRA", "REPUESTO"])
                i_cond = st.selectbox("Condición (Obligatorio FL para repuestos)", ["N/A", "Nuevo", "Usado", "Reconstruido"])
                i_desc = st.text_input("Descripción del trabajo o pieza")
                c1, c2, c3 = st.columns(3)
                with c1:
                    i_cant = st.number_input("Cantidad / Horas", min_value=0.5, value=1.0, step=0.5)
                with c2:
                    i_precio = st.number_input("Precio Unit. ($)", min_value=0.0, step=5.0)
                with c3:
                    i_fee = st.selectbox(
                        "Tasa Ambiental FL",
                        [0.0, 1.50, 1.00],
                        format_func=lambda x: "Ninguna ($0)" if x == 0 else ("Batería ($1.50)" if x == 1.5 else "Neumático ($1.00)"),
                    )

                if st.form_submit_button("Agregar Ítem", use_container_width=True):
                    if i_desc.strip():
                        p = ph()
                        execute_write(
                            f"""INSERT INTO items_orden (orden_id, tipo, condicion, descripcion, cantidad, precio_unitario, tasa_ambiental)
                                VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p})""",
                            (oid, i_tipo, i_cond, i_desc.strip(), float(i_cant), float(i_precio), float(i_fee)),
                        )
                        st.rerun()

        p = ph()
        df_items = read_df(f"SELECT * FROM items_orden WHERE orden_id = {p} ORDER BY id ASC", (oid,))

        if not df_items.empty:
            st.dataframe(
                df_items[["tipo", "condicion", "descripcion", "cantidad", "precio_unitario"]],
                use_container_width=True,
                hide_index=True,
            )
            sub_mo, sub_rep, tasas, tax, total = calcular_totales_florida(df_items)

            st.markdown(f"""
            * **Mano de Obra:** `${sub_mo:.2f}` | **Repuestos:** `${sub_rep:.2f}`
            * **FL Sales Tax ({TAX_RATE_FL*100:.1f}%):** `${tax:.2f}` *(Aplica sobre total si hay repuestos)*
            * ### Total: `${total:.2f}`
            """)

            presup_base = float(orden_actual["monto_presupuestado"] or 0.0)
            if presup_base > 0 and total > (presup_base * 1.10) and int(orden_actual["aprobacion_extra"] or 0) == 0:
                st.error(
                    f"⚠️ ALERTA LEY DE FLORIDA: El total (${total:.2f}) supera en más del 10% al presupuesto aprobado (${presup_base:.2f}). "
                    "Debes pedir autorización al cliente antes de facturar."
                )
                if st.button("✅ Marcar que el cliente autorizó el incremento por SMS/Llamada"):
                    execute_write(f"UPDATE ordenes SET aprobacion_extra = 1 WHERE id = {p}", (oid,))
                    st.rerun()

            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("📌 Fijar Presupuesto Base", use_container_width=True):
                    execute_write(
                        f"UPDATE ordenes SET monto_presupuestado = {p}, estado = 'Aprobado' WHERE id = {p}",
                        (float(total), oid),
                    )
                    st.success("Presupuesto fijado.")
                    st.rerun()

            with col_b:
                if st.button("📄 Generar PDF (WhatsApp)", use_container_width=True):
                    pdf_path = generar_pdf_orden(oid)
                    with open(pdf_path, "rb") as f:
                        st.download_button(
                            "⬇️ Descargar PDF para enviar",
                            f,
                            file_name=os.path.basename(pdf_path),
                            mime="application/pdf",
                            use_container_width=True,
                        )

            if orden_actual["estado"] != "Pagado":
                st.divider()
                st.markdown("**Cerrar Orden y Registrar Cobro**")
                odo_init = int(orden_actual["odometro_salida"] or 0)
                odo_out = st.number_input("Odómetro de Salida (mi)", min_value=odo_init, value=odo_init)
                metodo = st.selectbox("Método de Pago recibido", ["Zelle", "Efectivo", "Tarjeta / Square", "Transferencia"])
                if st.button("💰 Registrar Pago y Cerrar Orden", type="primary", use_container_width=True):
                    execute_write(
                        f"UPDATE ordenes SET estado = 'Pagado', odometro_salida = {p} WHERE id = {p}",
                        (int(odo_out), oid),
                    )
                    execute_write(
                        f"""INSERT INTO finanzas (fecha, tipo, categoria, descripcion, orden_id, subtotal, sales_tax, total, metodo_pago)
                            VALUES ({p}, 'INGRESO', 'Servicios Mecánicos', {p}, {p}, {p}, {p}, {p}, {p})""",
                        (
                            datetime.now().strftime("%Y-%m-%d"),
                            f"Cobro Orden #{oid} - {orden_actual['nombre']}",
                            oid,
                            float(sub_mo + sub_rep + tasas),
                            float(tax),
                            float(total),
                            metodo,
                        ),
                    )
                    st.success("🎉 Orden pagada e ingreso registrado en contabilidad.")
                    st.rerun()

# ------------------------------------------
# TAB 3: CONTROL DE ENTRADAS, SALIDAS Y TAXES
# ------------------------------------------
with menu[3]:
    st.subheader("Control Financiero y Fiscal (FL)")

    with st.expander("➖ Registrar Salida de Dinero (Gasto / Repuesto)"):
        with st.form("form_gasto", clear_on_submit=True):
            g_cat = st.selectbox(
                "Categoría (Deducible IRS)",
                ["Compra de Repuestos", "Herramientas y Equipos", "Combustible / Van", "Publicidad / Software", "Licencias / Seguros"],
            )
            g_desc = st.text_input("Detalle (Ej: AutoZone - Pastillas freno F-150)")
            g_monto = st.number_input("Monto Total Pagado ($)", min_value=0.01, step=5.0)
            g_met = st.selectbox("Pagado con", ["Tarjeta Débito Negocio", "Tarjeta Crédito Negocio", "Efectivo", "Zelle"])
            if st.form_submit_button("Guardar Gasto", use_container_width=True):
                p = ph()
                execute_write(
                    f"""INSERT INTO finanzas (fecha, tipo, categoria, descripcion, orden_id, subtotal, sales_tax, total, metodo_pago)
                        VALUES ({p}, 'EGRESO', {p}, {p}, NULL, {p}, 0.0, {p}, {p})""",
                    (
                        datetime.now().strftime("%Y-%m-%d"),
                        g_cat,
                        g_desc.strip(),
                        float(g_monto),
                        float(g_monto),
                        g_met,
                    ),
                )
                st.success("Gasto registrado.")
                st.rerun()

    df_fin = read_df("SELECT * FROM finanzas ORDER BY id DESC")

    if not df_fin.empty:
        ingresos = float(df_fin[df_fin["tipo"] == "INGRESO"]["total"].sum())
        egresos = float(df_fin[df_fin["tipo"] == "EGRESO"]["total"].sum())
        tax_recaudado = float(df_fin[df_fin["tipo"] == "INGRESO"]["sales_tax"].sum())
        utilidad_neta = (ingresos - tax_recaudado) - egresos

        c1, c2 = st.columns(2)
        c1.metric("Ingresos Totales", f"${ingresos:,.2f}")
        c2.metric("Gastos Totales", f"${egresos:,.2f}")

        c3, c4 = st.columns(2)
        c3.metric("Utilidad Real (Sin Tax)", f"${utilidad_neta:,.2f}")
        c4.metric(
            "FL Sales Tax a Declarar",
            f"${tax_recaudado:,.2f}",
            help="Este dinero pertenece al Florida Dept. of Revenue, no es ganancia.",
        )

        st.dataframe(
            df_fin[["fecha", "tipo", "categoria", "descripcion", "sales_tax", "total", "metodo_pago"]],
            use_container_width=True,
            hide_index=True,
        )

        csv = df_fin.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="📥 Exportar Contabilidad a CSV (Para Contador / QuickBooks)",
            data=csv,
            file_name=f"Contabilidad_Taller_FL_{datetime.now().strftime('%Y_%m')}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    else:
        st.info("Aún no hay movimientos financieros registrados.")