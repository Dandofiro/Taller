import os
import hmac
import sqlite3
from datetime import datetime

import pandas as pd
import streamlit as st
from fpdf import FPDF

try:
    import psycopg2
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False


# ==========================================
# BUSINESS CONFIGURATION (FLORIDA)
# ==========================================
SHOP_NAME = "EURO.SPORT96 LLC"
SHOP_ADDRESS = "Kissimmee, FL 34746"
SHOP_PHONE = "(321) 271-3612"
FDACS_REG = "MV-99999 (Pending / Enter your FL FDACS #)"
TAX_RATE_FL = 0.07  # 6% FL State + 1% County Discretionary (adjust to your county)
DB_FILE = "taller_florida.db"

st.set_page_config(page_title="Mobile Shop FL - Management", page_icon="🔧", layout="centered")

# ==========================================
# PASSWORD AUTHENTICATION
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

    st.title("🔐 Login - Mobile Shop FL")
    st.caption("Enter your password to manage appointments, work orders, and accounting.")

    with st.form("login_form"):
        pwd = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign In", use_container_width=True)

        if submitted:
            expected = get_app_password()
            if hmac.compare_digest(pwd, expected):
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")

    return False

if not check_login():
    st.stop()

# ==========================================
# DATABASE CONNECTION (POSTGRESQL / SQLITE)
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
            raise RuntimeError("Missing 'psycopg2-binary' to connect to PostgreSQL.") from exc
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
# FLORIDA TAX LOGIC (SALES TAX)
# ==========================================
def calcular_totales_florida(df_items: pd.DataFrame):
    """
    FDOR Florida Rule:
    - If the repair includes parts/materials, Sales Tax applies to the
      TOTAL (Labor + Parts + environmental fees).
    - If it is 100% labor with no parts, Sales Tax is $0.00.
    - Battery ($1.50) and tire ($1.00) environmental fees are added separately.
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
# FLORIDA LEGAL PDF GENERATOR
# ==========================================
def safe_pdf_text(text) -> str:
    return str(text or "").encode("latin-1", "replace").decode("latin-1")

def format_item_type_en(val: str) -> str:
    return "LABOR" if val == "MANO_OBRA" else ("PARTS" if val == "REPUESTO" else str(val or ""))

def format_condition_en(val: str) -> str:
    mapping = {
        "N/A": "N/A",
        "Nuevo": "New",
        "Usado": "Used",
        "Reconstruido": "Remanufactured",
    }
    return mapping.get(str(val or ""), str(val or ""))

def format_status_en(val: str) -> str:
    mapping = {
        "Pendiente": "Pending",
        "En Camino": "En Route",
        "Convertida a Orden": "Converted to Order",
        "Cancelada": "Cancelled",
        "Presupuesto": "Estimate",
        "Aprobado": "Approved",
        "Pagado": "Paid",
    }
    return mapping.get(str(val or ""), str(val or ""))

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
    pdf.cell(0, 8, safe_pdf_text(SHOP_NAME), ln=True)
    pdf.set_font("Arial", "", 9)
    pdf.cell(0, 5, safe_pdf_text(f"{SHOP_ADDRESS} | Phone: {SHOP_PHONE}"), ln=True)
    pdf.set_font("Arial", "B", 9)
    pdf.cell(0, 5, safe_pdf_text(f"FL Motor Vehicle Repair Reg #: {FDACS_REG}"), ln=True)
    pdf.ln(4)

    pdf.set_font("Arial", "B", 12)
    doc_tipo = "ESTIMATE" if orden["estado"] == "Presupuesto" else "INVOICE"
    estado_en = format_status_en(orden["estado"])
    pdf.cell(0, 8, safe_pdf_text(f"{doc_tipo} #00{orden_id} - Status: {estado_en}"), ln=True, border="B")
    pdf.ln(2)

    pdf.set_font("Arial", "", 9)
    pdf.cell(95, 5, safe_pdf_text(f"Customer: {orden['nombre']}"), ln=False)
    pdf.cell(95, 5, safe_pdf_text(f"Date In: {orden['fecha_ingreso']}"), ln=True)
    pdf.cell(95, 5, safe_pdf_text(f"Phone: {orden['telefono']}"), ln=False)
    pdf.cell(95, 5, safe_pdf_text(f"Promised Date: {orden['fecha_prometida']}"), ln=True)
    pdf.cell(95, 5, safe_pdf_text(f"Vehicle: {orden['marca_modelo_anio']}"), ln=False)
    pdf.cell(95, 5, safe_pdf_text(f"Plate / VIN: {orden['placa']} / {orden['vin']}"), ln=True)
    pdf.cell(95, 5, safe_pdf_text(f"Odometer In: {orden['odometro_entrada']} mi"), ln=False)
    pdf.cell(95, 5, safe_pdf_text(f"Odometer Out: {orden['odometro_salida']} mi"), ln=True)
    pdf.ln(4)

    pdf.set_font("Arial", "B", 9)
    pdf.cell(25, 6, "Type", border=1)
    pdf.cell(28, 6, "Condition", border=1)
    pdf.cell(82, 6, "Description", border=1)
    pdf.cell(15, 6, "Qty/Hrs", border=1, align="C")
    pdf.cell(20, 6, "Unit Price", border=1, align="R")
    pdf.cell(20, 6, "Total", border=1, align="R", ln=True)

    pdf.set_font("Arial", "", 8)
    for _, r in items.iterrows():
        tot_l = float(r["cantidad"]) * float(r["precio_unitario"])
        pdf.cell(25, 6, safe_pdf_text(format_item_type_en(r["tipo"])), border=1)
        pdf.cell(28, 6, safe_pdf_text(format_condition_en(r["condicion"])), border=1)
        pdf.cell(82, 6, safe_pdf_text(r["descripcion"])[:44], border=1)
        pdf.cell(15, 6, safe_pdf_text(r["cantidad"]), border=1, align="C")
        pdf.cell(20, 6, f"${float(r['precio_unitario']):.2f}", border=1, align="R")
        pdf.cell(20, 6, f"${tot_l:.2f}", border=1, align="R", ln=True)

    pdf.ln(3)
    pdf.set_font("Arial", "", 9)
    pdf.cell(150, 5, "Labor Subtotal:", align="R")
    pdf.cell(40, 5, f"${sub_mo:.2f}", align="R", ln=True)
    pdf.cell(150, 5, "Parts Subtotal:", align="R")
    pdf.cell(40, 5, f"${sub_rep:.2f}", align="R", ln=True)
    if tasas > 0:
        pdf.cell(150, 5, "FL Environmental Fees (Batteries/Tires):", align="R")
        pdf.cell(40, 5, f"${tasas:.2f}", align="R", ln=True)
    pdf.cell(150, 5, f"FL Sales Tax ({TAX_RATE_FL*100:.1f}%):", align="R")
    pdf.cell(40, 5, f"${tax:.2f}", align="R", ln=True)
    pdf.set_font("Arial", "B", 11)
    pdf.cell(150, 7, "TOTAL DUE (USD):", align="R")
    pdf.cell(40, 7, f"${total:.2f}", align="R", ln=True)

    pdf.ln(5)
    pdf.set_font("Arial", "B", 7)
    pdf.cell(0, 4, "FLORIDA STATUTE NOTICE (FS 559.905):", ln=True)
    pdf.set_font("Arial", "", 7)
    dev_txt = (
        "YES, customer requests return of replaced parts."
        if orden["devolucion_piezas"]
        else "NO, customer waives return of replaced parts."
    )
    pdf.multi_cell(0, 3.5, safe_pdf_text(
        f"1. Replaced Parts: {dev_txt}\n"
        "2. Under Florida law, the final charge may not exceed the written estimate by more than 10% without prior customer authorization.\n"
        "3. Warranty: 90 days or 3,000 miles on labor. Parts subject to manufacturer warranty."
    ))

    out_dir = "/tmp" if os.path.exists("/tmp") else "."
    safe_name = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in str(orden["nombre"]))
    filename = os.path.join(out_dir, f"Order_{orden_id}_{safe_name}.pdf")
    pdf.output(filename)
    return filename

# ==========================================
# HEADER AND LOGOUT
# ==========================================
col_t1, col_t2 = st.columns([4, 1])
with col_t1:
    st.title("🔧 Mobile Shop FL")
    st.caption("Database: PostgreSQL" if is_postgres() else "Database: Local SQLite")
with col_t2:
    st.write("")
    if st.button("Logout"):
        st.session_state["authenticated"] = False
        st.rerun()

menu = st.tabs(["📅 Schedule", "📋 New Order", "🛠️ Work Orders", "💵 Accounting"])

# ------------------------------------------
# TAB 0: APPOINTMENTS & ROUTES (MOBILE)
# ------------------------------------------
with menu[0]:
    st.subheader("Service Schedule")

    with st.expander("➕ Schedule New Appointment", expanded=False):
        with st.form("form_nueva_cita", clear_on_submit=True):
            col_f1, col_f2 = st.columns(2)
            with col_f1:
                cita_fecha = st.date_input("Visit date")
            with col_f2:
                cita_hora = st.time_input("Estimated time")

            cita_cli = st.text_input("Customer Name *")
            cita_tel = st.text_input("Phone (WhatsApp/SMS) *")
            cita_veh = st.text_input("Vehicle (Year, Make, Model) *", placeholder="e.g., 2020 Toyota Camry")
            cita_dir = st.text_input("Address / Vehicle Location", placeholder="e.g., 1234 NW 7th St, Miami, FL")
            cita_mot = st.text_area("Service / Reported Issue")

            if st.form_submit_button("Save Appointment", use_container_width=True):
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
                    st.success("✅ Appointment scheduled.")
                    st.rerun()
                else:
                    st.error("Please complete customer, phone, and vehicle (*).")

    ver_todas = st.checkbox("Show completed / cancelled appointments", value=False)
    query_citas = (
        "SELECT * FROM citas ORDER BY fecha ASC, hora ASC"
        if ver_todas
        else "SELECT * FROM citas WHERE estado IN ('Pendiente', 'En Camino') ORDER BY fecha ASC, hora ASC"
    )
    df_citas = read_df(query_citas)

    if df_citas.empty:
        st.info("No pending appointments on the schedule.")
    else:
        for _, c in df_citas.iterrows():
            cid = int(c["id"])
            estado_badge = format_status_en(c["estado"])
            with st.container(border=True):
                st.markdown(f"**🕒 {c['fecha']} — {c['hora']}** | `{estado_badge}`")
                st.markdown(f"**{c['cliente_nombre']}** ({c['telefono']}) — *{c['vehiculo']}*")
                if c["direccion"]:
                    st.caption(f"📍 {c['direccion']}")
                if c["motivo"]:
                    st.write(c["motivo"])

                col_b1, col_b2, col_b3 = st.columns(3)
                with col_b1:
                    if c["direccion"]:
                        maps_q = str(c["direccion"]).replace(" ", "+")
                        st.link_button("🗺️ Map", f"https://www.google.com/maps/search/?api=1&query={maps_q}", use_container_width=True)
                with col_b2:
                    if c["estado"] in ("Pendiente", "En Camino"):
                        if st.button("🔧 Start Order", key=f"conv_{cid}", use_container_width=True):
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
                            st.success("Work order created. Go to 'Work Orders' to enter odometer, parts, and labor.")
                            st.rerun()
                with col_b3:
                    if c["estado"] in ("Pendiente", "En Camino"):
                        if st.button("❌ Cancel", key=f"canc_{cid}", use_container_width=True):
                            p = ph()
                            execute_write(f"UPDATE citas SET estado = 'Cancelada' WHERE id = {p}", (cid,))
                            st.rerun()

# ------------------------------------------
# TAB 1: CREATE CUSTOMER, VEHICLE & ORDER
# ------------------------------------------
with menu[1]:
    st.subheader("Create Service / Estimate")
    with st.form("form_nueva_orden", clear_on_submit=True):
        c_nombre = st.text_input("Customer Name *")
        c_tel = st.text_input("Phone (WhatsApp/SMS) *")
        v_desc = st.text_input("Vehicle (Year, Make, Model) *", placeholder="e.g., 2018 Ford F-150")
        col1, col2 = st.columns(2)
        with col1:
            v_placa = st.text_input("License Plate")
            odo_in = st.number_input("Odometer In (mi) *", min_value=0, step=100)
        with col2:
            v_vin = st.text_input("VIN (Optional)")
            f_prom = st.date_input("Promised Date")

        dev_piezas = st.checkbox("Customer requests return of old parts (FL Law)")
        notas = st.text_area("Issue reported by customer")

        if st.form_submit_button("Create Work Order", use_container_width=True):
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
                st.success("✅ Order created. Go to 'Work Orders' to add parts and labor.")
            else:
                st.error("Please fill in all required fields (*).")

# ------------------------------------------
# TAB 2: MANAGE ITEMS, 10% RULE & PDF
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
        st.info("No work orders recorded yet.")
    else:
        opciones = {
            f"#{r['id']} - {r['nombre']} ({r['marca_modelo_anio']}) [{format_status_en(r['estado'])}]": int(r["id"])
            for _, r in df_ordenes.iterrows()
        }
        sel_label = st.selectbox("Select a Work Order:", list(opciones.keys()))
        oid = opciones[sel_label]
        orden_actual = df_ordenes[df_ordenes["id"] == oid].iloc[0]

        if int(orden_actual["odometro_entrada"] or 0) == 0:
            with st.expander("📏 Record Odometer In (Required in FL)", expanded=True):
                odo_in_edit = st.number_input("Current vehicle mileage", min_value=0, step=100, key=f"odoin_{oid}")
                if st.button("Save Odometer In", key=f"save_odoin_{oid}"):
                    p = ph()
                    execute_write(
                        f"UPDATE ordenes SET odometro_entrada = {p}, odometro_salida = {p} WHERE id = {p}",
                        (int(odo_in_edit), int(odo_in_edit), oid),
                    )
                    st.rerun()

        with st.expander("➕ Add Labor or Part", expanded=True):
            with st.form("add_item", clear_on_submit=True):
                i_tipo = st.selectbox(
                    "Type",
                    ["MANO_OBRA", "REPUESTO"],
                    format_func=format_item_type_en,
                )
                i_cond = st.selectbox(
                    "Condition (Required in FL for parts)",
                    ["N/A", "Nuevo", "Usado", "Reconstruido"],
                    format_func=format_condition_en,
                )
                i_desc = st.text_input("Job or part description")
                c1, c2, c3 = st.columns(3)
                with c1:
                    i_cant = st.number_input("Qty / Hours", min_value=0.5, value=1.0, step=0.5)
                with c2:
                    i_precio = st.number_input("Unit Price ($)", min_value=0.0, step=5.0)
                with c3:
                    i_fee = st.selectbox(
                        "FL Environmental Fee",
                        [0.0, 1.50, 1.00],
                        format_func=lambda x: "None ($0)" if x == 0 else ("Battery ($1.50)" if x == 1.5 else "Tire ($1.00)"),
                    )

                if st.form_submit_button("Add Item", use_container_width=True):
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
            df_items_view = df_items.copy()
            df_items_view["tipo"] = df_items_view["tipo"].map(format_item_type_en)
            df_items_view["condicion"] = df_items_view["condicion"].map(format_condition_en)
            df_items_view = df_items_view.rename(columns={
                "tipo": "Type",
                "condicion": "Condition",
                "descripcion": "Description",
                "cantidad": "Qty/Hrs",
                "precio_unitario": "Unit Price",
            })
            st.dataframe(
                df_items_view[["Type", "Condition", "Description", "Qty/Hrs", "Unit Price"]],
                use_container_width=True,
                hide_index=True,
            )
            sub_mo, sub_rep, tasas, tax, total = calcular_totales_florida(df_items)

            st.markdown(f"""
- **Labor:** `${sub_mo:.2f}` | **Parts:** `${sub_rep:.2f}`
- **FL Sales Tax ({TAX_RATE_FL*100:.1f}%):** `${tax:.2f}` *(Applies to total when parts are included)*
- ### Total: `${total:.2f}`
            """)

            presup_base = float(orden_actual["monto_presupuestado"] or 0.0)
            if presup_base > 0 and total > (presup_base * 1.10) and int(orden_actual["aprobacion_extra"] or 0) == 0:
                st.error(
                    f"⚠️ FLORIDA LAW ALERT: Total (${total:.2f}) exceeds the approved estimate (${presup_base:.2f}) by more than 10%. "
                    "You must obtain customer authorization before billing."
                )
                if st.button("✅ Mark that customer authorized the increase via SMS/Call"):
                    execute_write(f"UPDATE ordenes SET aprobacion_extra = 1 WHERE id = {p}", (oid,))
                    st.rerun()

            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("📌 Set Base Estimate", use_container_width=True):
                    execute_write(
                        f"UPDATE ordenes SET monto_presupuestado = {p}, estado = 'Aprobado' WHERE id = {p}",
                        (float(total), oid),
                    )
                    st.success("Estimate set.")
                    st.rerun()

            with col_b:
                if st.button("📄 Generate PDF (WhatsApp)", use_container_width=True):
                    pdf_path = generar_pdf_orden(oid)
                    with open(pdf_path, "rb") as f:
                        st.download_button(
                            "⬇️ Download PDF to send",
                            f,
                            file_name=os.path.basename(pdf_path),
                            mime="application/pdf",
                            use_container_width=True,
                        )

            if orden_actual["estado"] != "Pagado":
                st.divider()
                st.markdown("**Close Order and Record Payment**")
                odo_init = int(orden_actual["odometro_salida"] or 0)
                odo_out = st.number_input("Odometer Out (mi)", min_value=odo_init, value=odo_init)
                metodo = st.selectbox("Payment method received", ["Zelle", "Cash", "Card / Square", "Bank Transfer"])
                if st.button("💰 Record Payment & Close Order", type="primary", use_container_width=True):
                    execute_write(
                        f"UPDATE ordenes SET estado = 'Pagado', odometro_salida = {p} WHERE id = {p}",
                        (int(odo_out), oid),
                    )
                    execute_write(
                        f"""INSERT INTO finanzas (fecha, tipo, categoria, descripcion, orden_id, subtotal, sales_tax, total, metodo_pago)
                            VALUES ({p}, 'INGRESO', 'Mechanical Services', {p}, {p}, {p}, {p}, {p}, {p})""",
                        (
                            datetime.now().strftime("%Y-%m-%d"),
                            f"Payment Order #{oid} - {orden_actual['nombre']}",
                            oid,
                            float(sub_mo + sub_rep + tasas),
                            float(tax),
                            float(total),
                            metodo,
                        ),
                    )
                    st.success("🎉 Order paid and income recorded in accounting.")
                    st.rerun()

# ------------------------------------------
# TAB 3: INCOME, EXPENSES & TAXES
# ------------------------------------------
with menu[3]:
    st.subheader("Financial & Tax Control (FL)")

    with st.expander("➖ Record Expense (Part / Business Cost)"):
        with st.form("form_gasto", clear_on_submit=True):
            g_cat = st.selectbox(
                "Category (IRS Deductible)",
                ["Parts Purchase", "Tools & Equipment", "Fuel / Van", "Marketing / Software", "Licenses / Insurance"],
            )
            g_desc = st.text_input("Details (e.g., AutoZone - F-150 brake pads)")
            g_monto = st.number_input("Total Amount Paid ($)", min_value=0.01, step=5.0)
            g_met = st.selectbox("Paid with", ["Business Debit Card", "Business Credit Card", "Cash", "Zelle"])
            if st.form_submit_button("Save Expense", use_container_width=True):
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
                st.success("Expense recorded.")
                st.rerun()

    df_fin = read_df("SELECT * FROM finanzas ORDER BY id DESC")

    if not df_fin.empty:
        ingresos = float(df_fin[df_fin["tipo"] == "INGRESO"]["total"].sum())
        egresos = float(df_fin[df_fin["tipo"] == "EGRESO"]["total"].sum())
        tax_recaudado = float(df_fin[df_fin["tipo"] == "INGRESO"]["sales_tax"].sum())
        utilidad_neta = (ingresos - tax_recaudado) - egresos

        c1, c2 = st.columns(2)
        c1.metric("Total Income", f"${ingresos:,.2f}")
        c2.metric("Total Expenses", f"${egresos:,.2f}")

        c3, c4 = st.columns(2)
        c3.metric("Net Profit (Excl. Tax)", f"${utilidad_neta:,.2f}")
        c4.metric(
            "FL Sales Tax Payable",
            f"${tax_recaudado:,.2f}",
            help="This money belongs to the Florida Dept. of Revenue, not profit.",
        )

        df_fin_view = df_fin.copy()
        df_fin_view["tipo"] = df_fin_view["tipo"].map({"INGRESO": "INCOME", "EGRESO": "EXPENSE"}).fillna(df_fin_view["tipo"])
        df_fin_view = df_fin_view.rename(columns={
            "fecha": "Date",
            "tipo": "Type",
            "categoria": "Category",
            "descripcion": "Description",
            "sales_tax": "Sales Tax",
            "total": "Total",
            "metodo_pago": "Payment Method",
        })
        st.dataframe(
            df_fin_view[["Date", "Type", "Category", "Description", "Sales Tax", "Total", "Payment Method"]],
            use_container_width=True,
            hide_index=True,
        )

        csv = df_fin.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="📥 Export Accounting to CSV (For Accountant / QuickBooks)",
            data=csv,
            file_name=f"Accounting_Shop_FL_{datetime.now().strftime('%Y_%m')}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    else:
        st.info("No financial transactions recorded yet.")