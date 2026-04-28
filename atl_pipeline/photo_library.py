"""Verified-loading Unsplash photo IDs by industry. Each ID was load-tested.
Refresh by running `verify_photos.py` weekly — Unsplash sometimes invalidates IDs."""

PHOTOS = {
    'plumber': [
        ('1542013936693-884638332954', 'Diagnosing the line'),
        ('1530124566582-a618bc2615dc', 'Valves and couplings ready'),
        ('1521207418485-99c705420785', 'Modern install detail'),
        ('1517646287270-a5a9ca602e5c', 'Workshop close-up'),
        ('1659353588842-891391e6fcd4', 'Crew on site'),
        ('1707960190202-72087bace8cc', 'Equipment ready for the call'),
    ],
    'hvac': [
        ('1662556153586-223039dc8152', 'Tech checking gauges'),
        ('1660330589827-da8ab7dd3c02', 'Calibration in progress'),
        ('1561400555-786780284b67', 'Smart thermostat install'),
        ('1601659404194-97d2daca8383', 'Modern home comfort'),
        ('1642749776312-aa42ce20c9f5', 'Tools of the trade'),
        ('1561480337-03eb1b6795a2', 'Outdoor unit close-up'),
    ],
    'radiator': [
        ('1596986952526-3be237187071', 'In the shop'),
        ('1727893119356-1702fe921cf9', 'Engine bay inspection'),
        ('1615906655593-ad0386982a0f', 'Tools at the bench'),
        ('1727893294198-e85137574f5b', 'Vintage detail work'),
        ('1487754180451-c456f719a1fc', 'Classic in the garage'),
        ('1605822167835-d32696aef686', 'Mechanic at the bench'),
    ],
    'landscape': [
        ('1708432331128-cfe5a2803781', 'Pine straw delivery'),
        ('1725334775507-82772a52612a', 'Yard transformation'),
        ('1718565524318-b58b8b86b813', 'Crew at work'),
        ('1759577085348-7b7d9fb537a4', 'Materials on hand'),
        ('1766189790526-b699899d1013', 'Same-day finish'),
        ('1605822167835-d32696aef686', 'Hands-on work'),
    ],
    'septic': [
        ('1613051827322-1d547a68582d', 'On the road'),
        ('1652460197704-d4949761aeb6', 'Service vehicle'),
        ('1707960190202-72087bace8cc', 'Equipment ready'),
        ('1700616270841-078da5600853', 'Field crew'),
        ('1603477911780-59dcb83aecf8', 'Heavy-duty work'),
        ('1732193074634-0ac374288d9c', 'On-site dispatch'),
    ],
}

# Hero background photo (verified) per industry
HEROES = {
    'plumber':   '1551845728-6820a30c64e1',  # water drop
    'hvac':      '1631545806609-67caaa45f96b',
    'radiator':  '1502877338535-766e1452684a',
    'landscape': '1416879595882-3373a0480b5b',
    'septic':    '1530036846422-bb78d8e5fbc1',
}

CATEGORY_TO_INDUSTRY = {
    # plumbing variants
    'plumber': 'plumber', 'plumbing contractor': 'plumber',
    # hvac variants
    'hvac contractor': 'hvac', 'air conditioning contractor': 'hvac',
    'heating contractor': 'hvac', 'air duct cleaning service': 'hvac',
    'air conditioning system supplier': 'hvac',
    # auto / radiator
    'radiator repair service': 'radiator', 'radiator shop': 'radiator',
    # landscape
    'landscaping supply store': 'landscape', 'landscape': 'landscape',
    'tree service': 'landscape', 'christmas tree farm': 'landscape',
    # septic
    'septic service': 'septic',
}

def industry_for(category):
    if not category:
        return 'plumber'
    c = category.lower().strip()
    return CATEGORY_TO_INDUSTRY.get(c, 'plumber')

def img_url(photo_id, w=900):
    return f'https://images.unsplash.com/photo-{photo_id}?auto=format&fit=crop&w={w}&q=80'
