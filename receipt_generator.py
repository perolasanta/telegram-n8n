from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from datetime import datetime
import os

async def generate_receipt_pdf(order_data: dict, filename: str = None):
    """
    Generate PDF receipt for an order
    
    order_data structure:
    {
        'order_id': 'uuid',
        'restaurant_name': 'Demo Restaurant',
        'restaurant_phone': '08012345678',
        'table_number': '5',
        'customer_name': 'John Doe',
        'created_at': datetime,
        'items': [
            {'name': 'Jollof Rice', 'qty': 2, 'price': 1500, 'total': 3000},
            ...
        ],
        'subtotal': 5000,
        'tax': 0,
        'total': 5000,
        'payment_method': 'Cash Payment',
        'payment_status': 'confirmed'
    }
    """
    
    if not filename:
        filename = f"/tmp/receipt_{order_data['order_id'][:8]}.pdf"
    
    # Create PDF
    doc = SimpleDocTemplate(filename, pagesize=A4)
    elements = []
    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        textColor=colors.HexColor('#1a1a1a'),
        spaceAfter=30,
        alignment=TA_CENTER
    )
    
    heading_style = ParagraphStyle(
        'CustomHeading',
        parent=styles['Heading2'],
        fontSize=14,
        textColor=colors.HexColor('#333333'),
        spaceAfter=12
    )
    
    normal_style = styles['Normal']
    normal_style.fontSize = 10
    
    # Header - Restaurant Name
    elements.append(Paragraph(order_data['restaurant_name'], title_style))
    
    # Restaurant Info
    if order_data.get('restaurant_phone'):
        elements.append(Paragraph(f"Phone: {order_data['restaurant_phone']}", normal_style))
    elements.append(Spacer(1, 0.3*inch))
    
    # Receipt Title
    elements.append(Paragraph("RECEIPT", heading_style))
    elements.append(Spacer(1, 0.2*inch))
    
    # Order Details
    order_info = [
        ['Order ID:', f"#{order_data['order_id'][:8]}"],
        ['Date:', order_data['created_at'].strftime('%Y-%m-%d %H:%M:%S')],
        ['Table:', order_data['table_number']],
        ['Customer:', order_data['customer_name']],
    ]
    
    info_table = Table(order_info, colWidths=[2*inch, 4*inch])
    info_table.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), 'Helvetica', 10),
        ('FONT', (0, 0), (0, -1), 'Helvetica-Bold', 10),
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor('#333333')),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 0.3*inch))
    
    # Items Table
    elements.append(Paragraph("Order Items", heading_style))
    
    # Table header
    items_data = [['Item', 'Qty', 'Price', 'Total']]
    
    # Table rows
    for item in order_data['items']:
        items_data.append([
            item['name'],
            str(item['qty']),
            f"₦{item['price']:,.0f}",
            f"₦{item['total']:,.0f}"
        ])
    
    items_table = Table(items_data, colWidths=[3*inch, 0.8*inch, 1.2*inch, 1.2*inch])
    items_table.setStyle(TableStyle([
        # Header
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4CAF50')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 11),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        
        # Body
        ('FONT', (0, 1), (-1, -1), 'Helvetica', 10),
        ('ALIGN', (1, 1), (-1, -1), 'RIGHT'),
        ('ALIGN', (0, 1), (0, -1), 'LEFT'),
        
        # Grid
        ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#dddddd')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f9f9f9')]),
        
        # Padding
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 0.3*inch))
    
    # Totals
    totals_data = []
    
    if order_data.get('tax', 0) > 0:
        totals_data.append(['Subtotal:', f"₦{order_data['subtotal']:,.0f}"])
        totals_data.append(['Tax:', f"₦{order_data['tax']:,.0f}"])
    
    totals_data.append(['TOTAL:', f"₦{order_data['total']:,.0f}"])
    totals_data.append(['Payment Method:', order_data['payment_method']])
    totals_data.append(['Payment Status:', order_data['payment_status'].upper()])
    
    totals_table = Table(totals_data, colWidths=[4*inch, 2*inch])
    totals_table.setStyle(TableStyle([
        ('FONT', (0, 0), (0, -1), 'Helvetica-Bold', 11),
        ('FONT', (1, 0), (1, -1), 'Helvetica', 11),
        ('FONT', (0, -3), (-1, -3), 'Helvetica-Bold', 13),  # Total row
        ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LINEABOVE', (0, -3), (-1, -3), 2, colors.HexColor('#333333')),
    ]))
    elements.append(totals_table)
    elements.append(Spacer(1, 0.5*inch))
    
    # Footer
    footer_style = ParagraphStyle(
        'Footer',
        parent=styles['Normal'],
        fontSize=9,
        textColor=colors.HexColor('#666666'),
        alignment=TA_CENTER
    )
    elements.append(Paragraph("Thank you for your order!", footer_style))
    elements.append(Paragraph("Please come again!", footer_style))
    
    # Build PDF
    doc.build(elements)
    
    return filename