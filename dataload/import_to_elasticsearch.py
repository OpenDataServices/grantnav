import json
import argparse
import flattentool
import shutil
import uuid
import tempfile
import os
import csv
import gzip
from pprint import pprint
import elasticsearch.helpers
import requests
import time
import ijson
import dateutil.parser as date_parser


ES_INDEX = os.environ.get("ES_INDEX", "threesixtygiving")

id_name_org_mappings = {"fundingOrganization": {}, "recipientOrganization": {}}
name_duplicates = [["file_name", "org_type", "org_id", "first_name", "duplicate_name"]]
bad_org_ids = []

postcode_to_area = {}
district_code_to_area = {}
ward_code_to_area = {}
district_name_to_code = {}

current_dir = os.path.dirname(os.path.realpath(__file__))


def convert_spreadsheet(file_path, file_type, tmp_dir):
    #file_type = file_name.split('.')[-1]
    encoding = 'utf-8'
    converted_path = os.path.join(tmp_dir, 'output.json')
    if file_type == 'csv':
        destination = os.path.join(tmp_dir, 'grants.csv')
        shutil.copy(file_path, destination)
        try:
            with open(destination, encoding='utf-8') as main_sheet_file:
                main_sheet_file.read()
        except UnicodeDecodeError:
            try:
                with open(destination, encoding='cp1252') as main_sheet_file:
                    main_sheet_file.read()
                encoding = 'cp1252'
            except UnicodeDecodeError:
                encoding = 'latin_1'
        input_name = tmp_dir
    else:
        input_name = file_path
    try:
        flattentool.unflatten(
            input_name,
            output_name=converted_path,
            input_format=file_type,
            main_sheet_name='grants',
            root_list_path='grants',
            root_id='',
            schema='https://raw.githubusercontent.com/ThreeSixtyGiving/standard/master/schema/360-giving-schema.json',
            convert_titles=True,
            encoding=encoding
        )
    except Exception:
        print("Unflattening failed for file {}".format(file_path))
        raise

# curl http://test-360giving.pantheon.io/api/3/action/current_package_list_with_resources | grep -Eo '[^"]+\.json' | sed 's/\\\//\//g' | while read url; do wget "$url"; done


def maybe_create_index(index_name=ES_INDEX):
    """ Creates a new ES index based on value of ES_INDEX
    unless it already exists """
    es = elasticsearch.Elasticsearch()

    # Add the extra mapping info we want
    # (the rest will be auto inferred from the data we feed in)
    #
    # See issue #503 for why we do this for a non-standard field (Reference)
    mappings = {
        "properties": {
            "id": {"type": "keyword"},
            "filename": {"type": "keyword"},
            "title": {
                "type": "text", "analyzer": "english_with_folding"
            },
            "description": {
                "type": "text", "analyzer": "english_with_folding"
            },
            "recipientRegionName": {"type": "keyword"},
            "recipientDistrictName": {"type": "keyword"},
            "recipientWardName": {"type": "keyword"},
            "currency": {"type": "keyword"},
            "recipientLocation": {"type": "text"},
            "Reference": {"type": "keyword"},
            "title_and_description": {"type": "text", "analyzer": "english_with_folding"},
            "amountAppliedFor": {"type": "double"},
            "amountAwarded": {"type": "double"},
            "amountDisbursed": {"type": "double"},
            "awardDate": {
                "type": "date",
                "ignore_malformed": True
            },
            "dateModified": {"type": "keyword"},
            "plannedDates": {
                "properties": {
                    "startDate": {"type": "keyword"},
                    "endDate": {"type": "keyword"},
                    "duration": {"type": "text"}
                }
            },
            "recipientOrganization": {
                "properties": {
                    "addressLocality": {
                        "type": "keyword"
                    },
                    "charityNumber": {
                        "type": "keyword"
                    },
                    "companyNumber": {
                        "type": "keyword"
                    },
                    "id": {
                        "type": "keyword"
                    },
                    "url": {
                        "type": "keyword"
                    },
                    "name": {
                        "type": "text", "analyzer": "english_with_folding"
                    },
                    "streetAddress": {
                        "type": "text", "analyzer": "english_with_folding"
                    },
                    "id_and_name": {
                        "type": "keyword"
                    }
                }
            },
            "fundingOrganization": {
                "properties": {
                    "addressLocality": {
                        "type": "keyword"
                    },
                    "charityNumber": {
                        "type": "keyword"
                    },
                    "companyNumber": {
                        "type": "keyword"
                    },
                    "id": {
                        "type": "keyword"
                    },
                    "url": {
                        "type": "keyword"
                    },
                    "name": {
                        "type": "text", "analyzer": "english_with_folding"
                    },
                    "streetAddress": {
                        "type": "text", "analyzer": "english_with_folding"
                    },
                    "id_and_name": {
                        "type": "keyword"
                    }
                }
            },
            "beneficiaryLocation": {
                "properties": {
                    "geographic code (from GIFTS)": {"type": "text"}
                }
            }
        },
        "_default_": {
            "dynamic_templates": [{
                "unknown_to_strings": {
                    "match_mapping_type": "string",
                    "mapping": {
                        "type": "text",
                        "fielddata": True
                    }
                }
            }]
        },
    }

    settings = {
        "max_result_window": 500000,
        "analysis": {
            "analyzer": {
                # Based on the english analyzer decribed at
                # https://www.elastic.co/guide/en/elasticsearch/reference/2.4/analysis-lang-analyzer.html#english-analyzer
                "english_with_folding": {
                    "tokenizer": "standard",
                    "filter": [
                        # asciifolding not in the standard english analyzer.
                        "asciifolding",
                        "english_possessive_stemmer",
                        "lowercase",
                        "english_stop",
                        "english_stemmer",
                    ]
                }
            },
            "filter": {
                "english_stop": {
                    "type": "stop",
                    "stopwords": "_english_"
                },
                "english_stemmer": {
                    "type": "stemmer",
                    "language": "english"
                },
                "english_possessive_stemmer": {
                    "type": "stemmer",
                    "language": "possessive_english"
                }
            }
        }
    }

    # Create it
    result = es.indices.create(index=index_name, body={"mappings": mappings, "settings": settings}, ignore=[400])
    if 'error' in result and result['error']['reason'] == 'already exists':
        print('Updating existing index')
    else:
        pprint(result)


def import_to_elasticsearch(files, clean):

    es = elasticsearch.Elasticsearch()

    # Delete the index
    if clean:
        result = es.indices.delete(index=ES_INDEX, ignore=[404])
        pprint(result)

    maybe_create_index()

    time.sleep(1)

    with open(os.path.join(current_dir, 'charity_names.json')) as fd:
        charity_names = json.load(fd)
    id_name_org_mappings["recipientOrganization"].update(charity_names)

    with open(os.path.join(current_dir, 'primary_funding_org_name.json')) as fd:
        funding_org_name = json.load(fd)
    id_name_org_mappings["fundingOrganization"].update(funding_org_name)

    get_mapping_from_index(es)

    for file_name in files:
        tmp_dir = tempfile.mkdtemp()
        if file_name.startswith('http'):
            content = requests.get(file_name).content
            new_filename = file_name.split('/')[-1].split('?')[0]
            downloaded_filename = os.path.join(tmp_dir, new_filename)
            with open(downloaded_filename, 'wb+') as downloaded_file:
                downloaded_file.write(content)
            file_name = downloaded_filename

        file_type = file_name.split('.')[-1]

        if file_type == 'json':
            json_file_name = file_name
        elif file_type in ('csv', 'xlsx'):
            json_file_name = os.path.join(tmp_dir, 'output.json')
            convert_spreadsheet(file_name, file_type, tmp_dir)
        elif file_type in ('report'):
            continue
        else:
            print('unimportable file {} (bad) file type'.format(file_name))
            continue

        def grant_generator():
            with open(json_file_name) as fp:
                stream = ijson.items(fp, 'grants.item')
                for grant in stream:
                    grant['filename'] = file_name.strip('./')
                    grant['_id'] = str(uuid.uuid4())
                    grant['_index'] = ES_INDEX

                    update_doc_with_org_mappings(grant, "fundingOrganization", file_name)
                    update_doc_with_org_mappings(grant, "recipientOrganization", file_name)

                    # Save some time if GrantNav is receiving data from the
                    # datastore these fields will already be present so no need
                    # to add again.
                    if "additional_data_added" not in grant:
                        update_doc_with_region(grant)

                    update_doc_with_title_and_description(grant)
                    update_doc_with_dateonly_fields(grant)
                    currency = grant.get('currency')
                    if currency:
                        grant['currency'] = currency.upper()
                    yield grant

        pprint(file_name)
        result = elasticsearch.helpers.bulk(es, grant_generator(), raise_on_error=False)
        pprint(result)

        shutil.rmtree(tmp_dir)


def get_mapping_from_index(es):
    MAX_INT = 2147483647
    QUERY = {"query": {"match_all": {}},
             "aggs": {
                 "fundingOrganization": {"terms": {"field": "fundingOrganization.id_and_name", "size": MAX_INT}},
                 "recipientOrganization": {"terms": {"field": "recipientOrganization.id_and_name", "size": MAX_INT}}}}
    results = es.search(body=QUERY, index=ES_INDEX)
    for bucket in results["aggregations"]["fundingOrganization"]["buckets"]:
        id_name = json.loads(bucket["key"])
        id_name_org_mappings["fundingOrganization"][id_name[0]] = id_name[1]

    for bucket in results["aggregations"]["recipientOrganization"]["buckets"]:
        id_name = json.loads(bucket["key"])
        id_name_org_mappings["recipientOrganization"][id_name[0]] = id_name[1]


def add_area_to_grant(area, grant):
    if area.get('ward_code'):
        grant['recipientWardNameGeoCode'] = area['ward_code']
        grant['recipientWardName'] = ward_code_to_area.get(area['ward_code'], {}).get('ward_name')
    if area['district_name']:
        grant['recipientDistrictName'] = area['district_name']
        grant['recipientDistrictGeoCode'] = district_name_to_code.get(area['district_name'])
    if area['area_name']:
        grant['recipientRegionName'] = area['area_name']

    grant['recipientLocation'] = ' '.join(area.values())


def update_doc_with_region(grant):
    try:
        post_code = grant['recipientOrganization'][0]['postalCode']
    except (KeyError, IndexError):
        post_code = ''

    # If there is a 'BT' postcode we can safely assume this is in NI
    if post_code and post_code.startswith("BT"):
        grant['recipientRegionName'] = "Northern Ireland"

    # test postcode first
    area = postcode_to_area.get(str(post_code).replace(' ', '').upper())
    if area:
        add_area_to_grant(area, grant)
        return

    if not area:
        try:
            locations = grant['recipientOrganization'][0]['location']
        except (KeyError, IndexError):
            return

        # then test ward
        for location in locations:
            geoCode = location.get('geoCode')
            if geoCode and geoCode in ward_code_to_area:
                add_area_to_grant(ward_code_to_area.get(geoCode), grant)
                return

        # finally district
        for location in locations:
            geoCode = location.get('geoCode')
            if geoCode and geoCode in district_code_to_area:
                add_area_to_grant(district_code_to_area.get(geoCode), grant)
                return
            # No NI data but try and get name from data
            if geoCode and geoCode.startswith("N09"):
                grant['recipientRegionName'] = "Northern Ireland"
                grant['recipientDistrictName'] = location["name"]


def update_doc_with_title_and_description(grant):
    """
    Update ElasticSearch with the new key 'title_and_description'.
    """
    description = grant.get('description') if grant.get('description') else ''
    title = grant.get('title') if grant.get('title') else ''
    grant['title_and_description'] = title + ' ' + description


def update_doc_with_org_mappings(grant, org_key, file_name):
    mapping = id_name_org_mappings[org_key]
    orgs = grant.get(org_key, [])
    for org in orgs:
        org_id, name = org.get('id'), org.get('name')
        if not name:
            name = org_id
        if not org_id:
            return
        if '/' in org_id:
            bad_org_ids.append([file_name, org_key, org_id])

        found_name = mapping.get(org_id)
        if found_name:
            if found_name != name:
                name_duplicates.append([file_name, org_key, org_id, found_name, name])
        else:
            mapping[org_id] = name
            found_name = name
        org["id_and_name"] = json.dumps([found_name, org_id])


def update_doc_with_dateonly_fields(grant):
    """
    If possible parse the date to only show the date part
    rather than the full ISO date
    """
    def add_dateonly(parent, key):
        try:
            datetime = date_parser.parse(parent.get(key))
            parent[key + 'DateOnly'] = datetime.date().isoformat()
        except (ValueError, TypeError):
            parent[key + 'DateOnly'] = parent.get(key)

    add_dateonly(grant, 'awardDate')

    for dates in grant.get('plannedDates', []) + grant.get('actualDates', []):
        add_dateonly(dates, 'startDate')
        add_dateonly(dates, 'endDate')


def get_area_mappings():
    with open(os.path.join(current_dir, 'codelist.csv')) as codelist, gzip.open(os.path.join(current_dir, 'codepoint_with_heading.csv.gz'), 'rt') as codepoint:
        codelist_csv = csv.DictReader(codelist)
        code_to_name = {}
        for row in codelist_csv:
            code_to_name[row['code']] = row['name']
        
        codepoint_csv = csv.DictReader(codepoint)

        for row in codepoint_csv:
            district_code = row['Admin_district_code']
            district_name = code_to_name.get(district_code, '').replace(' (B)', '')
            ward_code = row['Admin_ward_code']
            ward_name = code_to_name.get(ward_code, '')

            regional_code = row['NHS_HA_code']
            area_name = ''
            if not regional_code or regional_code[0] != 'E':
                country_code = row['Country_code']
                first_letter = country_code[0]
                if first_letter == 'S':
                    area_name = 'Scotland'
                if first_letter == 'W':
                    area_name = 'Wales'
            else:
                area_name = code_to_name[regional_code]

            postcode_to_area[row['Postcode'].replace(' ', '').upper()] = {
                'district_code': district_code,
                'district_name': district_name,
                'area_name': area_name,
                'ward_name': ward_name,
                'ward_code': ward_code
            }
            district_code_to_area[district_code] = {
                'district_code': district_code,
                'district_name': district_name,
                'area_name': area_name
            }
            ward_code_to_area[ward_code] = {
                'district_code': district_code,
                'district_name': district_name,
                'area_name': area_name,
                'ward_name': ward_name,
                'ward_code': ward_code
            }

            district_name_to_code[district_name] = district_code

    # Northern Ireland codes and names not included in Code-Point, but uses a separate source
    with open(os.path.join(current_dir, 'WD15_LGD15_NI_LU.csv')) as ni_lookup:
        ni_lookup_csv = csv.DictReader(ni_lookup)

        for row in ni_lookup_csv:
            district_code = row['LGD15CD']
            district_name = row['LGD15NM'].replace(' (B)', '')
            ward_code = row['WD15CD']
            ward_name = row['WD15NM']
            area_name = 'Northern Ireland'

            district_code_to_area[district_code] = {
                'district_code': district_code,
                'district_name': district_name,
                'area_name': area_name
            }
            ward_code_to_area[ward_code] = {
                'district_code': district_code,
                'district_name': district_name,
                'area_name': area_name,
                'ward_name': ward_name,
                'ward_code': ward_code
            }

            district_name_to_code[district_name] = district_code


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Import 360 files in a directory to elasticsearch')
    parser.add_argument('--clean', help='Delete existing data before import', action='store_true')
    parser.add_argument('--reports', help='Generate reports of differing names and bad organisation IDs.', action='store_true')
    parser.add_argument('files', help='files to import', nargs='+')
    args = parser.parse_args()
    get_area_mappings()
    import_to_elasticsearch(args.files, args.clean)
    if args.reports:
        with open("differing_names.csv.report", "w+") as differing_names_file:
            csv_writer = csv.writer(differing_names_file)
            csv_writer.writerows(name_duplicates)
        with open("bad_org_ids.csv.report", "w+") as bad_org_ids_file:
            csv_writer = csv.writer(bad_org_ids_file)
            csv_writer.writerows(bad_org_ids)
