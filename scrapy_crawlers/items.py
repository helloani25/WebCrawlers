import scrapy


class PropertyItem(scrapy.Item):
    # Canonical identity
    source = scrapy.Field()
    source_listing_id = scrapy.Field()
    mls_id = scrapy.Field()
    detail_url = scrapy.Field()

    # Canonical location
    address = scrapy.Field()
    city = scrapy.Field()
    state = scrapy.Field()
    postal_code = scrapy.Field()
    county = scrapy.Field()
    latitude = scrapy.Field()
    longitude = scrapy.Field()

    # Canonical listing facts
    list_price = scrapy.Field()
    status = scrapy.Field()
    property_type = scrapy.Field()
    property_sub_type = scrapy.Field()
    beds = scrapy.Field()
    baths = scrapy.Field()
    full_baths = scrapy.Field()
    half_baths = scrapy.Field()
    sqft = scrapy.Field()
    living_area_sqft = scrapy.Field()
    lot_size_sqft = scrapy.Field()
    lot_size_acres = scrapy.Field()
    year_built = scrapy.Field()
    stories = scrapy.Field()
    days_on_market = scrapy.Field()
    garage_spaces = scrapy.Field()
    heating = scrapy.Field()
    cooling = scrapy.Field()
    construction_materials = scrapy.Field()
    foundation_details = scrapy.Field()
    exterior_features = scrapy.Field()
    tax_annual_amount = scrapy.Field()
    tax_year = scrapy.Field()
    description = scrapy.Field()

    # Canonical brokerage/media
    listing_agent = scrapy.Field()
    listing_office = scrapy.Field()
    listing_office_phone = scrapy.Field()
    photos_count = scrapy.Field()
    first_photo_url = scrapy.Field()
    photo_links = scrapy.Field()

    # Metadata
    canonical_schema_version = scrapy.Field()
    parse_status = scrapy.Field()
    validation_errors = scrapy.Field()
    source_fields = scrapy.Field()
    raw_listing = scrapy.Field()
