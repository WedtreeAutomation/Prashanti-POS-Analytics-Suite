import streamlit as st
import xmlrpc.client
from datetime import datetime, timedelta
from collections import defaultdict
import xlsxwriter
import io
import re
import os
from dotenv import load_dotenv
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd
import time

# Load environment variables
load_dotenv()

# === Odoo Configuration from Environment ===
CONFIG = {
    'url': st.secrets["ODOO_URL"],
    'db': st.secrets["ODOO_DB"],
    'username': st.secrets["ODOO_USERNAME"],
    'password': st.secrets["ODOO_PASSWORD"],
    'timeout': int(st.secrets["ODOO_TIMEOUT"]),
    'order_batch_size': int(st.secrets["ORDER_BATCH_SIZE"]),
    'read_batch_size': int(st.secrets["READ_BATCH_SIZE"])
}

# === Helper Functions ===
def format_mobile_number(mobile):
    """Add +91 prefix to mobile number and clean it"""
    if not mobile:
        return ""
    # Remove any non-digit characters
    cleaned = re.sub(r'[^\d]', '', str(mobile))
    # Remove leading 0 if present
    if cleaned.startswith('0'):
        cleaned = cleaned[1:]
    # Add +91 prefix if not already present
    if not cleaned.startswith('91') and len(cleaned) == 10:
        return f"+91{cleaned}"
    elif len(cleaned) == 12 and cleaned.startswith('91'):
        return f"+{cleaned}"
    else:
        return mobile

# === Custom Transport with Timeout ===
class TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout=None, use_datetime=False):
        super().__init__(use_datetime=use_datetime)
        self.timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self.timeout
        return conn

# === Odoo Connection ===
@st.cache_resource
def connect_to_odoo():
    transport = TimeoutTransport(timeout=CONFIG['timeout'])
    common = xmlrpc.client.ServerProxy(CONFIG['url'] + 'xmlrpc/2/common', transport=transport, allow_none=True)
    uid = common.authenticate(CONFIG['db'], CONFIG['username'], CONFIG['password'], {})
    models = xmlrpc.client.ServerProxy(CONFIG['url'] + 'xmlrpc/2/object', transport=transport, allow_none=True)
    return uid, models

BRANCH_KEYWORDS = {
    "CBE": ["CB", "CBE"],
    "TN": ["TN"],
    "MLM": ["MLM"],
    "HYD": ["HYD"],
    "JYR": ["JYR"],
    "Vizag": ["Vizag", "VZG"],
    "Saree Trails": ["PUNE"]
}

def fetch_pos_configs(models, uid, branch_name):
    """Fetch POS configs that belong strictly to the selected branch."""
    try:
        keywords = BRANCH_KEYWORDS.get(branch_name, [branch_name])
        search_domains = []
        for kw in keywords:
            search_domains.append(['name', 'ilike', kw])
        # Special handling for Saree Trails
        if branch_name == "Saree Trails":
            search_domains.append(['name', 'ilike', 'Local Expo'])
        if not search_domains:
            return []
        domain = ['|'] * (len(search_domains) - 1) + search_domains
        configs = models.execute_kw(
            CONFIG['db'], uid, CONFIG['password'],
            'pos.config', 'search_read',
            [domain],
            {'fields': ['id', 'name']}
        )
        if not configs:
            return []
        filtered_configs = []
        for config in configs:
            if not isinstance(config, dict):
                continue
            name = config.get('name', '').strip().lower()
            if not name:
                continue
            # For Local Expo configs - must contain exact branch keyword
            if "local expo" in name and branch_name == "Saree Trails":
                if any(f' {kw.lower()} ' in f' {name} ' for kw in keywords):
                    filtered_configs.append(config)
            else:
                starts_with_keyword = any(name.startswith(kw.lower()) for kw in keywords)
                contains_keyword = any(f' {kw.lower()} ' in f' {name} ' for kw in keywords)
                if starts_with_keyword or contains_keyword:
                    filtered_configs.append(config)
        return filtered_configs
    except Exception as e:
        st.error(f"Error fetching POS configs: {str(e)}")
        return []

def fetch_order_ids(models, uid, config_ids, from_date, to_date):
    try:
        order_ids = []
        offset = 0
        progress_bar = st.progress(0)
        status_text = st.empty()
        while True:
            batch = models.execute_kw(
                CONFIG['db'], uid, CONFIG['password'],
                'pos.order', 'search',
                [[
                    ['config_id', 'in', config_ids],
                    ['date_order', '>=', from_date.strftime('%Y-%m-%d %H:%M:%S')],
                    ['date_order', '<=', to_date.strftime('%Y-%m-%d %H:%M:%S')],
                    ['state', '=', 'done']
                ]],
                {'offset': offset, 'limit': CONFIG['order_batch_size']}
            )
            if not batch:
                break
            order_ids.extend(batch)
            offset += len(batch)
            progress = min(offset / (offset + 100), 0.95)
            progress_bar.progress(progress)
            status_text.text(f"🔍 Fetched {len(order_ids)} order IDs...")
        progress_bar.progress(1.0)
        status_text.empty()
        return order_ids
    except Exception as e:
        st.error(f"Error fetching order IDs: {str(e)}")
        return []

def fetch_order_details(models, uid, order_ids):
    try:
        orders = []
        progress_bar = st.progress(0)
        status_text = st.empty()
        for i in range(0, len(order_ids), CONFIG['read_batch_size']):
            batch_ids = order_ids[i:i+CONFIG['read_batch_size']]
            batch_orders = models.execute_kw(
                CONFIG['db'], uid, CONFIG['password'],
                'pos.order', 'read',
                [batch_ids],
                {'fields': ['partner_id', 'amount_total', 'date_order', 'pos_reference', 'config_id', 'lines']}
            )
            if batch_orders and isinstance(batch_orders, list):
                orders.extend(batch_orders)
            progress = (i + len(batch_ids)) / len(order_ids)
            progress_bar.progress(progress)
            status_text.text(f"📊 Processing order details... {i + len(batch_ids)}/{len(order_ids)}")
        progress_bar.progress(1.0)
        status_text.empty()
        return orders
    except Exception as e:
        st.error(f"Error fetching order details: {str(e)}")
        return []

def fetch_related_data(models, uid, orders):
    try:
        partner_ids = []
        config_ids = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            # Safely get partner_id
            partner_id_field = order.get('partner_id')
            if isinstance(partner_id_field, list) and len(partner_id_field) > 0:
                partner_id = partner_id_field[0]
            elif isinstance(partner_id_field, int):
                partner_id = partner_id_field
            else:
                partner_id = None
            # Safely get config_id
            config_id_field = order.get('config_id')
            if isinstance(config_id_field, list) and len(config_id_field) > 0:
                config_id = config_id_field[0]
            elif isinstance(config_id_field, int):
                config_id = config_id_field
            else:
                config_id = None
            if partner_id:
                partner_ids.append(partner_id)
            if config_id:
                config_ids.append(config_id)
        partner_ids = list(set(partner_ids))
        config_ids = list(set(config_ids))
        partners = []
        if partner_ids:
            for i in range(0, len(partner_ids), CONFIG['read_batch_size']):
                batch_ids = partner_ids[i:i+CONFIG['read_batch_size']]
                batch_partners = models.execute_kw(
                    CONFIG['db'], uid, CONFIG['password'],
                    'res.partner', 'read',
                    [batch_ids],
                    {'fields': ['name', 'mobile', 'email']}
                )
                if batch_partners and isinstance(batch_partners, list):
                    partners.extend(batch_partners)
        configs = []
        if config_ids:
            batch_configs = models.execute_kw(
                CONFIG['db'], uid, CONFIG['password'],
                'pos.config', 'read',
                [config_ids],
                {'fields': ['name']}
            )
            if batch_configs and isinstance(batch_configs, list):
                configs = batch_configs
        # Format mobile numbers
        for partner in partners:
            if isinstance(partner, dict) and 'mobile' in partner:
                partner['mobile'] = format_mobile_number(partner['mobile'])
        return {p['id']: p for p in partners if isinstance(p, dict)}, {c['id']: c for c in configs if isinstance(c, dict)}
    except Exception as e:
        st.error(f"Error fetching related data: {str(e)}")
        return {}, {}

def generate_excel(orders, partner_dict, config_dict, from_date, to_date, branch_name):
    try:
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        # Styles
        title_fmt = workbook.add_format({'bold': True, 'font_size': 16, 'align': 'center', 'valign': 'vcenter', 'font_color': '#2c3e50', 'bottom': 6})
        subtitle_fmt = workbook.add_format({'italic': True, 'align': 'center', 'valign': 'vcenter', 'font_color': '#7f8c8d', 'bottom': 2})
        header_fmt = workbook.add_format({'bold': True, 'bg_color': '#3498db', 'font_color': 'white', 'border': 1, 'align': 'center', 'valign': 'vcenter', 'text_wrap': True})
        date_fmt = workbook.add_format({'num_format': 'dd-mm-yyyy hh:mm:ss', 'align': 'center'})
        money_fmt = workbook.add_format({'num_format': '₹#,##0.00', 'align': 'right'})
        center_fmt = workbook.add_format({'align': 'center'})
        highlight_fmt = workbook.add_format({'bg_color': '#f8f9fa', 'border': 1})

        # === Order Details Sheet ===
        sheet = workbook.add_worksheet("Order Details")
        sheet.set_column('A:I', 18)
        sheet.merge_range('A1:I1', f"Prashanti Sarees - {branch_name} Branch", title_fmt)
        sheet.merge_range('A2:I2', f"POS Orders from {from_date} to {to_date}", subtitle_fmt)

        headers = ["Order Date", "Order Reference", "POS Terminal", "Customer ID", "Customer Name", "Mobile", "Email", "Amount (₹)", "Config ID"]
        sheet.write_row(3, 0, headers, header_fmt)

        for row_num, order in enumerate(orders, 4):
            if not isinstance(order, dict):
                continue
            # Safely get partner_id
            partner_id_field = order.get('partner_id')
            if isinstance(partner_id_field, list) and len(partner_id_field) > 0:
                partner_id = partner_id_field[0]
            elif isinstance(partner_id_field, int):
                partner_id = partner_id_field
            else:
                partner_id = None
            partner = partner_dict.get(partner_id, {}) if partner_id else {}

            # Safely get config_id
            config_id_field = order.get('config_id')
            if isinstance(config_id_field, list) and len(config_id_field) > 0:
                config_id = config_id_field[0]
            elif isinstance(config_id_field, int):
                config_id = config_id_field
            else:
                config_id = None
            config = config_dict.get(config_id, {}) if config_id else {}

            row_fmt = highlight_fmt if row_num % 2 == 0 else None
            try:
                order_date = datetime.strptime(order.get('date_order', ''), "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                order_date = datetime.now()
            sheet.write(row_num, 0, order_date, date_fmt)
            sheet.write(row_num, 1, order.get('pos_reference', ''), center_fmt)
            sheet.write(row_num, 2, config.get('name', ''), row_fmt)
            sheet.write(row_num, 3, partner_id or "", center_fmt)
            sheet.write(row_num, 4, partner.get('name', ''), row_fmt)
            sheet.write(row_num, 5, partner.get('mobile', ''), center_fmt)
            sheet.write(row_num, 6, partner.get('email', ''), row_fmt)
            sheet.write(row_num, 7, order.get('amount_total', 0), money_fmt)
            sheet.write(row_num, 8, config_id or "", center_fmt)

        last_row = len(orders) + 4
        sheet.write(last_row, 6, "TOTAL:", header_fmt)
        sheet.write_formula(last_row, 7, f"=SUM('Order Details'!H5:H{last_row})", 
            workbook.add_format({'num_format': '₹#,##0.00', 'bold': True, 'bg_color': '#3498db', 'font_color': 'white'}))

        # === Customer Summary Sheet ===
        summary_sheet = workbook.add_worksheet("Customer Summary")
        summary_sheet.set_column('A:G', 20)
        summary_sheet.merge_range('A1:G1', f"Prashanti Sarees - {branch_name} Branch", title_fmt)
        summary_sheet.merge_range('A2:G2', f"Customer Summary from {from_date} to {to_date}", subtitle_fmt)

        customer_data = defaultdict(lambda: {"name": "", "mobile": "", "email": "", "count": 0, "total": 0.0})
        for order in orders:
            if not isinstance(order, dict):
                continue
            # Safely get partner_id
            partner_id_field = order.get('partner_id')
            if isinstance(partner_id_field, list) and len(partner_id_field) > 0:
                partner_id = partner_id_field[0]
            elif isinstance(partner_id_field, int):
                partner_id = partner_id_field
            else:
                partner_id = None

            partner = partner_dict.get(partner_id, {}) if partner_id else {}

            # Safely get config_id
            config_id_field = order.get('config_id')
            if isinstance(config_id_field, list) and len(config_id_field) > 0:
                config_id = config_id_field[0]
            elif isinstance(config_id_field, int):
                config_id = config_id_field
            else:
                config_id = None

            if partner_id:
                customer_data[partner_id]["name"] = partner.get('name', '')
                customer_data[partner_id]["mobile"] = partner.get('mobile', '')
                customer_data[partner_id]["email"] = partner.get('email', '')
                customer_data[partner_id]["count"] += 1
                customer_data[partner_id]["total"] += order.get('amount_total', 0)

        summary_headers = ["Customer ID", "Customer Name", "Mobile", "Email", "Order Count", "Total Amount (₹)", "Avg Amount (₹)"]
        summary_sheet.write_row(3, 0, summary_headers, header_fmt)

        for row_idx, (partner_id, data) in enumerate(customer_data.items(), 4):
            avg_amount = data['total'] / data['count'] if data['count'] > 0 else 0
            row_fmt = highlight_fmt if row_idx % 2 == 0 else None
            summary_sheet.write(row_idx, 0, partner_id, center_fmt)
            summary_sheet.write(row_idx, 1, data['name'], row_fmt)
            summary_sheet.write(row_idx, 2, data['mobile'], center_fmt)
            summary_sheet.write(row_idx, 3, data['email'], row_fmt)
            summary_sheet.write(row_idx, 4, data['count'], center_fmt)
            summary_sheet.write(row_idx, 5, data['total'], money_fmt)
            summary_sheet.write(row_idx, 6, avg_amount, money_fmt)

        last_row = len(customer_data) + 4
        summary_sheet.write(last_row, 3, "TOTAL:", header_fmt)
        summary_sheet.write_formula(last_row, 4, f"=SUM('Customer Summary'!E5:E{last_row})", workbook.add_format({'bold': True, 'align': 'center'}))
        summary_sheet.write_formula(last_row, 5, f"=SUM('Customer Summary'!F5:F{last_row})", workbook.add_format({'num_format': '₹#,##0.00', 'bold': True, 'bg_color': '#3498db', 'font_color': 'white'}))

        # === Sales Analysis Sheet ===
        analysis_sheet = workbook.add_worksheet("Sales Analysis")
        analysis_sheet.set_column('A:D', 20)
        analysis_sheet.merge_range('A1:D1', f"Prashanti Sarees - {branch_name} Branch", title_fmt)
        analysis_sheet.merge_range('A2:D2', f"Sales Analysis from {from_date} to {to_date}", subtitle_fmt)

        date_data = defaultdict(lambda: {"count": 0, "total": 0.0})
        for order in orders:
            if not isinstance(order, dict):
                continue
            try:
                date_obj = datetime.strptime(order.get('date_order', ''), "%Y-%m-%d %H:%M:%S")
                date_only = date_obj.date()
                date_data[date_only]["count"] += 1
                date_data[date_only]["total"] += order.get('amount_total', 0)
            except (ValueError, TypeError):
                continue

        sorted_dates = sorted(date_data.keys())
        analysis_headers = ["Date", "Order Count", "Total Revenue (₹)", "Avg Order Value (₹)"]
        analysis_sheet.write_row(3, 0, analysis_headers, header_fmt)

        for row_idx, date in enumerate(sorted_dates, 4):
            data = date_data[date]
            avg_value = data['total'] / data['count'] if data['count'] > 0 else 0
            row_fmt = highlight_fmt if row_idx % 2 == 0 else None
            analysis_sheet.write(row_idx, 0, date.strftime('%d-%m-%Y'), row_fmt)
            analysis_sheet.write(row_idx, 1, data['count'], center_fmt)
            analysis_sheet.write(row_idx, 2, data['total'], money_fmt)
            analysis_sheet.write(row_idx, 3, avg_value, money_fmt)

        if sorted_dates:
            chart = workbook.add_chart({'type': 'column'})
            chart.add_series({
                'name': "='Sales Analysis'!$C$4",
                'categories': f"='Sales Analysis'!$A$5:$A${len(sorted_dates)+4}",
                'values': f"='Sales Analysis'!$C$5:$C${len(sorted_dates)+4}",
                'fill': {'color': '#3498db'},
            })

            chart2 = workbook.add_chart({'type': 'line'})
            chart2.add_series({
                'name': "='Sales Analysis'!$D$4",
                'categories': f"='Sales Analysis'!$A$5:$A${len(sorted_dates)+4}",
                'values': f"='Sales Analysis'!$D$5:$D${len(sorted_dates)+4}",
                'y2_axis': True,
                'line': {'color': '#e74c3c', 'width': 2.5},
            })

            chart.combine(chart2)
            chart.set_title({'name': 'Daily Sales Performance'})
            chart.set_x_axis({'name': 'Date'})
            chart.set_y_axis({'name': 'Revenue (₹)'})
            chart.set_y2_axis({'name': 'Avg Order (₹)'})

            analysis_sheet.insert_chart('F5', chart, {'x_scale': 2, 'y_scale': 1.5})

        workbook.close()
        output.seek(0)
        return output
    except Exception as e:
        st.error(f"Error generating Excel report: {str(e)}")
        raise

def create_analytics_dashboard(orders, partner_dict, config_dict):
    if not orders:
        return
    df_data = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        # Safely get partner_id
        partner_id_field = order.get('partner_id')
        if isinstance(partner_id_field, list) and len(partner_id_field) > 0:
            partner_id = partner_id_field[0]
        elif isinstance(partner_id_field, int):
            partner_id = partner_id_field
        else:
            partner_id = None
        # Safely get config_id
        config_id_field = order.get('config_id')
        if isinstance(config_id_field, list) and len(config_id_field) > 0:
            config_id = config_id_field[0]
        elif isinstance(config_id_field, int):
            config_id = config_id_field
        else:
            config_id = None
        try:
            order_date = datetime.strptime(order['date_order'], "%Y-%m-%d %H:%M:%S")
            df_data.append({
                'date': order_date.date(),
                'datetime': order_date,
                'amount': order.get('amount_total', 0),
                'customer': partner_dict.get(partner_id, {}).get('name', 'Unknown') if partner_id else 'Walk-in Customer',
                'terminal': config_dict.get(config_id, {}).get('name', 'Unknown') if config_id else 'Unknown',
                'has_customer': bool(partner_id)
            })
        except (ValueError, TypeError):
            continue
    if not df_data:
        return
    df = pd.DataFrame(df_data)
    
    st.markdown("## 📊 Analytics Dashboard")
    
    with st.spinner('Calculating metrics...'):
        time.sleep(1)
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.markdown(f"""
        <div style='background: linear-gradient(135deg, #f6e58d, #ffbe76); 
                    padding: 1rem; border-radius: 10px; text-align: center;'>
            <h3 style='color: #2c3e50; margin: 0;'>🛒 Total Orders</h3>
            <h1 style='color: #2c3e50; margin: 0.5rem 0;'>{len(orders):,}</h1>
        </div>
        """, unsafe_allow_html=True)
    
    with col2:
        total_revenue = df['amount'].sum()
        st.markdown(f"""
        <div style='background: linear-gradient(135deg, #badc58, #6ab04c); 
                    padding: 1rem; border-radius: 10px; text-align: center;'>
            <h3 style='color: white; margin: 0;'>💰 Total Revenue</h3>
            <h1 style='color: white; margin: 0.5rem 0;'>₹{total_revenue:,.2f}</h1>
        </div>
        """, unsafe_allow_html=True)
    
    with col3:
        unique_customers = df[df['has_customer']]['customer'].nunique()
        st.markdown(f"""
        <div style='background: linear-gradient(135deg, #ff7979, #eb4d4b); 
                    padding: 1rem; border-radius: 10px; text-align: center;'>
            <h3 style='color: white; margin: 0;'>👥 Unique Customers</h3>
            <h1 style='color: white; margin: 0.5rem 0;'>{unique_customers:,}</h1>
        </div>
        """, unsafe_allow_html=True)
    
    with col4:
        avg_order_value = df['amount'].mean()
        st.markdown(f"""
        <div style='background: linear-gradient(135deg, #7ed6df, #22a6b3); 
                    padding: 1rem; border-radius: 10px; text-align: center;'>
            <h3 style='color: white; margin: 0;'>📈 Avg Order Value</h3>
            <h1 style='color: white; margin: 0.5rem 0;'>₹{avg_order_value:,.2f}</h1>
        </div>
        """, unsafe_allow_html=True)
    
    st.markdown("---")
    col1, col2 = st.columns(2)
    
    with col1:
        daily_sales = df.groupby('date')['amount'].sum().reset_index()
        fig_trend = px.line(
            daily_sales, 
            x='date', 
            y='amount',
            title='📈 Daily Sales Trend',
            labels={'amount': 'Revenue (₹)', 'date': 'Date'},
            template='plotly_white'
        )
        fig_trend.update_layout(
            hovermode='x unified',
            plot_bgcolor='rgba(0,0,0,0)',
            paper_bgcolor='rgba(0,0,0,0)',
        )
        fig_trend.update_xaxes(
            rangeslider_visible=True,
            rangeselector=dict(
                buttons=list([
                    dict(count=1, label="1m", step="month", stepmode="backward"),
                    dict(count=3, label="3m", step="month", stepmode="backward"),
                    dict(count=6, label="6m", step="month", stepmode="backward"),
                    dict(step="all")
                ])
            )
        )
        st.plotly_chart(fig_trend, use_container_width=True)
    
    with col2:
        df['hour'] = df['datetime'].dt.hour
        hourly_sales = df.groupby(['date', 'hour'])['amount'].sum().reset_index()
        fig_heatmap = px.density_heatmap(
            hourly_sales,
            x='hour',
            y='date',
            z='amount',
            title='🕒 Hourly Sales Heatmap',
            labels={'hour': 'Hour of Day', 'date': 'Date', 'amount': 'Revenue'},
            template='plotly_white',
            color_continuous_scale='Viridis'
        )
        st.plotly_chart(fig_heatmap, use_container_width=True)
    
    col1, col2 = st.columns(2)
    
    with col1:
        terminal_sales = df.groupby('terminal')['amount'].sum().reset_index()
        fig_terminal = px.pie(
            terminal_sales,
            values='amount',
            names='terminal',
            title='🖥️ Sales by Terminal',
            hole=0.4,
            template='plotly_white'
        )
        fig_terminal.update_traces(textposition='inside', textinfo='percent+label')
        st.plotly_chart(fig_terminal, use_container_width=True)
    
    with col2:
        customer_type = df['has_customer'].map({True: 'Registered', False: 'Walk-in'})
        fig_customer = px.pie(
            names=customer_type.value_counts().index,
            values=customer_type.value_counts().values,
            title='👥 Customer Type Distribution',
            hole=0.4,
            template='plotly_white'
        )
        fig_customer.update_traces(textposition='inside', textinfo='percent+label')
        st.plotly_chart(fig_customer, use_container_width=True)
    
    if unique_customers > 0:
        st.markdown("### 🌟 Top Customers")
        customer_stats = df[df['has_customer']].groupby('customer').agg({
            'amount': ['sum', 'count', 'mean']
        }).round(2)
        customer_stats.columns = ['Total Spent (₹)', 'Order Count', 'Avg Order (₹)']
        customer_stats = customer_stats.sort_values('Total Spent (₹)', ascending=False).head(10)
        
        fig_top_customers = px.bar(
            customer_stats.reset_index(),
            x='customer',
            y='Total Spent (₹)',
            title='🏆 Top Customers by Spending',
            text='Total Spent (₹)',
            template='plotly_white'
        )
        fig_top_customers.update_traces(marker_color='#3498db')
        fig_top_customers.update_layout(xaxis_title="Customer", yaxis_title="Total Spent (₹)")
        st.plotly_chart(fig_top_customers, use_container_width=True)

# === Streamlit UI ===
def main():
    st.set_page_config(
        page_title="Prashanti Sarees - POS Reports",
        page_icon="🏪",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    st.markdown("""
    <style>
        .stApp {
            background-color: white;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        
        .header {
            text-align: center;
            padding: 2rem 0;
            margin-bottom: 2rem;
            border-bottom: 1px solid #e0e0e0;
            background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
            border-radius: 0 0 15px 15px;
        }
        
        .header h1 {
            font-size: 2.5rem;
            color: #2c3e50;
            margin-bottom: 0.5rem;
        }
        
        .header p {
            color: #666;
            font-size: 1.1rem;
        }
        
        .card {
            background: white;
            border-radius: 10px;
            padding: 1.5rem;
            margin: 1rem 0;
            box-shadow: 0 4px 15px rgba(0,0,0,0.05);
            border: 1px solid #eee;
            transition: all 0.3s ease;
        }
        
        .card:hover {
            transform: translateY(-3px);
            box-shadow: 0 6px 20px rgba(0,0,0,0.1);
        }
        
        .stButton > button {
            background: linear-gradient(135deg, #3498db 0%, #2980b9 100%);
            color: white;
            border: none;
            border-radius: 8px;
            padding: 0.75rem 1.5rem;
            font-weight: 500;
            transition: all 0.3s ease;
            box-shadow: 0 2px 5px rgba(0,0,0,0.1);
        }
        
        .stButton > button:hover {
            background: linear-gradient(135deg, #2980b9 0%, #3498db 100%);
            transform: translateY(-2px);
            box-shadow: 0 4px 8px rgba(0,0,0,0.15);
        }
        
        .stSelectbox, .stDateInput, .stTextInput {
            border-radius: 8px;
        }
        
        .section-header {
            font-size: 1.4rem;
            color: #2c3e50;
            margin: 1.5rem 0 1rem 0;
            padding-bottom: 0.5rem;
            border-bottom: 2px solid #eee;
        }
        
        .css-1d391kg {
            background-color: white;
            border-right: 1px solid #eee;
        }
        
        .stProgress > div > div {
            background: linear-gradient(135deg, #3498db 0%, #2ecc71 100%);
        }
        
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }
        
        .fade-in {
            animation: fadeIn 0.5s ease-out;
        }
    </style>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div class="header fade-in">
        <h1>🏪 Prashanti Sarees</h1>
        <p>POS Order Analytics & Reporting System</p>
    </div>
    """, unsafe_allow_html=True)
    
    with st.sidebar:
        st.markdown("### ⚙️ Configuration")
        st.info("Configure your Odoo connection in the `.env` file")
        
        st.markdown("### 📅 Quick Date Presets")
        preset = st.selectbox(
            "Choose preset:",
            ["Custom", "Today", "Yesterday", "Last 7 days", "Last 30 days", "This Month", "Last Month"],
            key="date_preset"
        )
        
        today = datetime.now().date()
        if preset == "Today":
            from_date = to_date = today
        elif preset == "Yesterday":
            from_date = to_date = today - timedelta(days=1)
        elif preset == "Last 7 days":
            from_date = today - timedelta(days=7)
            to_date = today
        elif preset == "Last 30 days":
            from_date = today - timedelta(days=30)
            to_date = today
        elif preset == "This Month":
            from_date = today.replace(day=1)
            to_date = today
        elif preset == "Last Month":
            last_month = today.replace(day=1) - timedelta(days=1)
            from_date = last_month.replace(day=1)
            to_date = last_month
        else:
            from_date = st.date_input("From Date", datetime(2024, 1, 1), key="from_date")
            to_date = st.date_input("To Date", today, key="to_date")
    
    st.markdown('<div class="section-header">🏢 Branch Selection</div>', unsafe_allow_html=True)
    
    branch_options = ["TN", "CBE", "MLM", "HYD", "JYR", "Vizag", "Saree Trails"]
    branch = st.selectbox(
        "Select Branch:",
        options=branch_options,
        index=0,
        help="Select the branch to generate report for",
        key="branch_select"
    )
    
    pos_configs = []
    try:
        uid, models = connect_to_odoo()
        pos_configs = fetch_pos_configs(models, uid, branch)
    except Exception as e:
        st.warning(f"Could not fetch POS configurations: {str(e)}")
    
    st.markdown('<div class="section-header">🖥️ POS Terminal Configuration</div>', unsafe_allow_html=True)
    
    with st.container():
        st.info(f"POS terminals available for **{branch}** branch:")
        
        if not pos_configs:
            st.warning("No POS terminals found for this branch!")
        else:
            cols = st.columns(3)
            for i, config in enumerate(pos_configs[:3]):
                with cols[i % 3]:
                    with st.container():
                        st.markdown(f"""
                        <div class="card">
                            <h3>Terminal {i+1}</h3>
                            <p>{config['name']}</p>
                        </div>
                        """, unsafe_allow_html=True)
            
            selected_configs = st.multiselect(
                "Select terminals to include:",
                options=[config['name'] for config in pos_configs],
                default=[config['name'] for config in pos_configs],
                help="Select which POS terminals to include in the report"
            )
    
    st.markdown('<div class="section-header">🚀 Generate Report</div>', unsafe_allow_html=True)
    
    if st.button("✨ Generate Report", type="primary", use_container_width=True):
        if not selected_configs:
            st.error("Please select at least one POS terminal!")
            return
            
        with st.spinner("Connecting to Odoo and fetching data..."):
            try:
                uid, models = connect_to_odoo()
                
                with st.empty():
                    st.success("✅ Connected to Odoo successfully!")
                    time.sleep(1)
                
                config_ids = [config['id'] for config in pos_configs if config['name'] in selected_configs]
                
                if not config_ids:
                    st.error("No POS configurations selected!")
                    return
                
                with st.spinner(f"Fetching orders for {len(config_ids)} terminals..."):
                    order_ids = fetch_order_ids(models, uid, config_ids, from_date, to_date)
                    
                    if not order_ids:
                        st.warning("No orders found in the specified date range.")
                        st.info("Try expanding your date range or check if there were any sales during this period.")
                        return
                    
                    orders = fetch_order_details(models, uid, order_ids)
                    partner_dict, config_dict = fetch_related_data(models, uid, orders)
                    
                    success_message = st.empty()
                    success_message.success(f"🎉 Successfully processed **{len(orders)}** orders for **{branch}** branch!")
                    time.sleep(1)
                    
                    create_analytics_dashboard(orders, partner_dict, config_dict)
                    
                    with st.expander("📋 Data Preview (First 10 Orders)", expanded=False):
                        preview_data = []
                        for o in orders[:10]:
                            partner_id = o.get('partner_id', [None])[0]
                            partner = partner_dict.get(partner_id, {})
                            config_id = o.get('config_id', [None])[0]
                            config = config_dict.get(config_id, {})
                            
                            preview_data.append({
                                "📅 Date": o['date_order'][:10],
                                "🔖 Reference": o['pos_reference'],
                                "🖥️ Terminal": config.get('name', 'N/A'),
                                "👤 Customer": partner.get('name', 'Walk-in Customer'),
                                "📱 Mobile": partner.get('mobile', 'N/A'),
                                "💰 Amount": f"₹{o['amount_total']:,.2f}"
                            })
                        
                        st.dataframe(preview_data, use_container_width=True)
                    
                    with st.spinner("Generating Excel report..."):
                        excel_file = generate_excel(orders, partner_dict, config_dict, from_date, to_date, branch)
                        time.sleep(1)
                    
                    st.markdown('<div class="section-header">⬇️ Download Reports</div>', unsafe_allow_html=True)
                    
                    col1, col2, col3 = st.columns([1, 2, 1])
                    with col2:
                        st.download_button(
                            label="📊 Download Excel Report",
                            data=excel_file,
                            file_name=f"prashanti_sarees_{branch}_{from_date}_{to_date}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            help=f"Complete Excel report for {branch} branch",
                            use_container_width=True
                        )
                    
                    st.markdown('<div class="section-header">📈 Summary Statistics</div>', unsafe_allow_html=True)
                    
                    total_amount = sum(order.get('amount_total', 0) for order in orders)
                    customers_with_data = len([o for o in orders if o.get('partner_id')])
                    avg_order = total_amount / len(orders) if orders else 0
                    
                    col1, col2, col3, col4 = st.columns(4)
                    
                    with col1:
                        st.markdown(f"""
                        <div class="card">
                            <h3 style='color: #7f8c8d; margin: 0;'>📊 Total Orders</h3>
                            <h1 style='color: #2c3e50; margin: 0.5rem 0; text-align: center;'>{len(orders):,}</h1>
                        </div>
                        """, unsafe_allow_html=True)
                    
                    with col2:
                        st.markdown(f"""
                        <div class="card">
                            <h3 style='color: #7f8c8d; margin: 0;'>💰 Total Revenue</h3>
                            <h1 style='color: #2c3e50; margin: 0.5rem 0; text-align: center;'>₹{total_amount:,.2f}</h1>
                        </div>
                        """, unsafe_allow_html=True)
                    
                    with col3:
                        st.markdown(f"""
                        <div class="card">
                            <h3 style='color: #7f8c8d; margin: 0;'>👥 Customer Orders</h3>
                            <h1 style='color: #2c3e50; margin: 0.5rem 0; text-align: center;'>{customers_with_data:,}</h1>
                        </div>
                        """, unsafe_allow_html=True)
                    
                    with col4:
                        st.markdown(f"""
                        <div class="card">
                            <h3 style='color: #7f8c8d; margin: 0;'>📈 Avg Order</h3>
                            <h1 style='color: #2c3e50; margin: 0.5rem 0; text-align: center;'>₹{avg_order:,.2f}</h1>
                        </div>
                        """, unsafe_allow_html=True)
                    
            except Exception as e:
                st.error(f"❌ Error generating report: {str(e)}")
                st.info("""
                **Troubleshooting Tips:**
                - Check your internet connection
                - Verify Odoo credentials in .env file
                - Ensure the Odoo server is running
                - Check if the selected date range has data
                """)

    st.markdown("---")
    st.markdown("""
    <div style="text-align: center; padding: 2rem; color: #666; animation: fadeIn 1s ease-out;">
        <p>🏪 <strong>Prashanti Sarees</strong> - POS Analytics System</p>
        <p>Version 2.1 | Dynamic POS Terminal Loading | Enhanced Reporting</p>
    </div>
    """, unsafe_allow_html=True)

if __name__ == "__main__":
    main()
