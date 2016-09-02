import pytest
import requests
import urllib.parse
from dataload.import_to_elasticsearch import import_to_elasticsearch, get_area_mappings
import json
import time

prefix = 'https://raw.githubusercontent.com/OpenDataServices/grantnav-sampledata/5259c973c4f89f054a18be8f1143202d250bc148/'


@pytest.fixture(scope="module")
def dataload():
    get_area_mappings()
    import_to_elasticsearch([prefix + 'a002400000KeYdsAAF.json'], clean=True)
    #elastic search needs some time to commit its data
    time.sleep(2)


@pytest.fixture(scope="function")
def provenance_dataload(dataload, settings, tmpdir):
    local_data_json = tmpdir.join('data.json')
    local_data_json.write(requests.get(prefix + 'data.json').content)
    settings.PROVENANCE_JSON = local_data_json.strpath


@pytest.mark.parametrize(('expected_text'), [
    ('GrantNav'),
    ('Search'),
    ('grantmaking'),
    ('Try these examples'),
    ('Only Locations'),
    ('Bristol'),
    ('Only Recipients'),
    ('children'),
    ('"arts centre"'),
    ('"360G-wolfson-19750"')
    ])
def test_home(provenance_dataload, client, expected_text):
    response = client.get('/')
    assert expected_text in str(response.content)
    assert "grant-making" not in str(response.content)


def test_search(provenance_dataload, client):
    initial_response = client.get('/search?text_query=gardens')
    assert initial_response.status_code == 302
    response = client.get(initial_response.url)
    assert "Total grants" in str(response.content)
    assert "The British Museum" in str(response.content)

    assert response.context['text_query'] == 'gardens'
    assert response.context['results']['hits']['total'] == 8

    # click facet
    wolfson_facet = response.context['results']['aggregations']['fundingOrganization']['buckets'][0]
    assert wolfson_facet['doc_count'] == 8
    assert wolfson_facet['key'] == '["Wolfson Foundation", "GB-CHC-1156077"]'
    json_query = json.loads(urllib.parse.parse_qs(initial_response.url.split('?')[-1])['json_query'][0])

    json_query['query']['bool']['filter'][0]['bool']['should'] = [{'term': {'fundingOrganization.id_and_name': '["Wolfson Foundation", "GB-CHC-1156077"]'}}]
    assert json.loads(urllib.parse.parse_qs(wolfson_facet['url'].split('?')[-1])['json_query'][0]) == json_query

    # click agian
    response = client.get(wolfson_facet['url'])
    wolfson_facet = response.context['results']['aggregations']['fundingOrganization']['buckets'][0]
    assert wolfson_facet['doc_count'] == 8
    assert wolfson_facet['key'] == '["Wolfson Foundation", "GB-CHC-1156077"]'
    json_query['query']['bool']['filter'][0]['bool']['should'] = []
    assert json.loads(urllib.parse.parse_qs(wolfson_facet['url'].split('?')[-1])['json_query'][0]) == json_query


def test_stats(provenance_dataload, client):
    response = client.get('/stats')
    assert "379" in str(response.content)


def test_advanced_search(provenance_dataload, client):
    response = client.get('/help')
    assert 'Advanced Search' in str(response.content)
