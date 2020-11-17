import collections
import copy
import csv
import datetime
import json
import math
import re
from itertools import chain

import dateutil.parser as date_parser
import elasticsearch.exceptions
from django.conf import settings
from django.http import Http404, JsonResponse
from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import render, redirect
from django.utils.http import urlencode
from elasticsearch.helpers import scan

from grantnav import provenance, csv_layout, utils
from grantnav.search import get_es
from grantnav.index import get_index

# Filters used for each of the facets that we use. Order of this list must be maintained.
BASIC_FILTER = [
    {"bool": {"should": []}},  # Funding Orgs
    {"bool": {"should": []}},  # Recipient Orgs
    {"bool": {"should": [], "must": {}, "minimum_should_match": 1}},  # Amount Awarded Fixed
    {"bool": {"should": {"range": {"amountAwarded": {}}}, "must": {}, "minimum_should_match": 1}},  # Amount Awarded
    {"bool": {"should": []}},  # Award Year
    {"bool": {"should": []}},  # additional_data.recipientRegionName
    {"bool": {"should": []}},  # additional_data.recipientDistrictName
    {"bool": {"should": []}}   # currency
]

TermFacet = collections.namedtuple('TermFacet', 'field_name param_name filter_index display_name is_json is_dropdown')

TERM_FACETS = [
    TermFacet("fundingOrganization.id_and_name", "fundingOrganization", 0, "Funders", True, True),
    TermFacet("recipientOrganization.id_and_name", "recipientOrganization", 1, "Recipients", True, True),
    TermFacet("additional_data.recipientRegionName", "recipientRegionName", 5, "Regions", False, False),
    TermFacet("additional_data.recipientDistrictName", "recipientDistrictName", 6, "Districts", False, True),
    TermFacet("currency", "currency", 7, "Currency", False, True)
]

DROPDOWN_SIZE = 5000
LESS_SIZE = 3
MORE_SIZE = 50
SIZE = 20

BASIC_QUERY = {"query": {"bool": {"must":
                                  {"query_string": {"query": "", "default_field": "*"}}, "filter": BASIC_FILTER}},
               "extra_context": {"awardYear_facet_size": 3, "amountAwardedFixed_facet_size": 3},
               "sort": {"_score": {"order": "desc"}},
               "aggs": {}}

for term_facet in TERM_FACETS:
    BASIC_QUERY['aggs'][term_facet.param_name] = {"terms": {"field": term_facet.field_name,
                                                            "size": DROPDOWN_SIZE if term_facet.is_dropdown else LESS_SIZE}}

FIXED_AMOUNT_RANGES = [
    {"from": 0, "to": 500},
    {"from": 500, "to": 1000},
    {"from": 1000, "to": 5000},
    {"from": 5000, "to": 10000},
    {"from": 10000, "to": 50000},
    {"from": 50000, "to": 100000},
    {"from": 100000, "to": 500000},
    {"from": 1000000, "to": 10000000},
    {"from": 10000000}
]


SEARCH_SUMMARY_AGGREGATES = {
    "recipient_orgs": {"cardinality": {"field": "recipientOrganization.id", "precision_threshold": 40000}},
    "funding_orgs": {"cardinality": {"field": "fundingOrganization.id", "precision_threshold": 40000}},
    "currency_stats": {"terms": {"field": "currency"}, "aggs": {"amount_stats": {"stats": {"field": "amountAwarded"}}}},
    "min_date": {"min": {"field": "awardDate"}},
    "max_date": {"max": {"field": "awardDate"}}
}


def clean_object(obj):
    for key, value in list(obj.items()):
        if isinstance(value, dict):
            clean_object(value)
        elif isinstance(value, list):
            clean_array(value)
        if isinstance(value, (dict, list)) and not value:
            obj.pop(key, None)


def clean_array(array):
    new_array = []
    for item in array:
        if isinstance(item, dict):
            clean_object(item)
        elif isinstance(item, list):
            clean_array(item)
        if isinstance(item, (dict, list)) and item:
            new_array.append(item)
    array[:] = new_array
    return array


def clean_for_es(json_query):
    clean_object(json_query)
    return json_query


def get_results(json_query, size=10, from_=0):
    es = get_es()
    extra_context = json_query.pop('extra_context', None)

    new_json_query = clean_for_es(copy.deepcopy(json_query))

    results = es.search(body=new_json_query, size=size, from_=from_,
                        index=get_index(), track_total_hits=True)
    if extra_context is not None:
        json_query['extra_context'] = extra_context
    return results


def get_request_type_and_size(request):
    results_size = SIZE

    match = re.search(r'\.(\w+)$', request.path)
    if match and match.group(1) in ["csv", "json", "ajax"]:
        result_format = match.group(1)
        results_size = settings.FLATTENED_DOWNLOAD_LIMIT
    else:
        result_format = "html"

    return [result_format, results_size]


def get_data_from_path(path, data):
    current_pos = data
    for part in path.split("."):
        try:
            part = int(part)
        except ValueError:
            pass
        try:
            current_pos = current_pos[part]
        except (KeyError, IndexError, TypeError):
            return ""
    return current_pos


def grants_csv_generator(query):
    yield csv_layout.grant_csv_titles
    es = get_es()
    for result in scan(es, query, index=get_index()):
        result_with_provenance = {
            "result": result["_source"],
            "dataset": provenance.by_identifier.get(provenance.identifier_from_filename(result['_source']['filename']), {})
        }
        line = []
        for path in csv_layout.grant_csv_paths:
            line.append(get_data_from_path(path, result_with_provenance))
        yield line


def grants_json_generator(query):
    yield '''{
    "license": "See dataset/license within each grant. This file also contains OS data © Crown copyright and database right 2016, Royal Mail data © Royal Mail copyright and Database right 2016, National Statistics data © Crown copyright and database right 2016, see http://grantnav.org/datasets/ for more information.",
    "grants": [\n'''
    es = get_es()
    for num, result in enumerate(scan(es, query, index=get_index())):
        result["_source"]["dataset"] = provenance.by_identifier.get(provenance.identifier_from_filename(result['_source']['filename']), {})
        if num == 0:
            yield json.dumps(result["_source"]) + "\n"
        else:
            yield ", " + json.dumps(result["_source"]) + "\n"
    yield ']}'


class Echo(object):
    def write(self, value):
        return value


def grants_csv_paged(query):
    query.pop('extra_context', None)
    query.pop('aggs', None)
    pseudo_buffer = Echo()
    writer = csv.writer(pseudo_buffer)
    response = StreamingHttpResponse(chain(['\ufeff'], (writer.writerow(row) for row in grants_csv_generator(clean_for_es(query)))), content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="grantnav-{0}.csv"'.format(datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
    return response


def grants_json_paged(query):
    query.pop('extra_context', None)
    query.pop('aggs', None)
    response = StreamingHttpResponse(grants_json_generator(clean_for_es(query)), content_type='application/json')
    response['Content-Disposition'] = 'attachment; filename="grantnav-{0}.json"'.format(datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
    return response


def org_csv_generator(data, org_type):
    if org_type == 'funder':
        yield csv_layout.funder_csv_titles
    elif org_type == 'recipient':
        yield csv_layout.recipient_csv_titles

    for result in data:
        line = []
        for path in csv_layout.org_csv_paths:
            line.append(get_data_from_path(path, result))
        yield line


def orgs_csv_paged(data, org_type):
    pseudo_buffer = Echo()
    writer = csv.writer(pseudo_buffer)
    response = StreamingHttpResponse(chain(['\ufeff'], (writer.writerow(row) for row in org_csv_generator(data, org_type))), content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="grantnav-{0}.csv"'.format(datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
    return response


def get_pagination(request, context, page):
    total_pages = math.ceil(context['results']['hits']['total']['value'] / SIZE)
    if page < total_pages:
        context['next_page'] = request.path + '?' + create_parameters_from_json_query(context['query'], page=page + 1)
    if page != 1 and total_pages > 1:
        context['prev_page'] = request.path + '?' + create_parameters_from_json_query(context['query'], page=page - 1)


def get_terms_facet_size(request, context, json_query, page):
    json_query = copy.deepcopy(json_query)
    see_more_url = {}
    try:
        aggs = json_query["aggs"]
    except KeyError:
        aggs = BASIC_QUERY['aggs']
    for agg_name, agg in aggs.items():
        new_aggs = copy.deepcopy(aggs)
        if "terms" not in agg:
            continue
        size = agg["terms"]["size"]
        if size == DROPDOWN_SIZE:
            continue
        if size == LESS_SIZE:
            new_size = MORE_SIZE
            see_more_url[agg_name] = {"more": True}
        else:
            new_size = LESS_SIZE
            see_more_url[agg_name] = {"more": False}
        new_aggs[agg_name]["terms"]["size"] = new_size

        json_query["aggs"] = new_aggs
        see_more_url[agg_name]["url"] = request.path + '?' + create_parameters_from_json_query(json_query, page=page) + '#' + agg_name

    context['see_more_url'] = see_more_url


def get_non_terms_facet_size(request, context, json_query, page, agg_name):
    see_more = {}
    new_json_query = copy.deepcopy(json_query)
    facet_size = new_json_query['extra_context'][agg_name + '_facet_size']
    if facet_size == LESS_SIZE:
        facet_size = MORE_SIZE
        see_more["more"] = True
    else:
        facet_size = LESS_SIZE
        see_more["more"] = False

    new_json_query['extra_context'][agg_name + '_facet_size'] = facet_size
    new_url = request.path + '?' + create_parameters_from_json_query(new_json_query, page=page) + '#' + agg_name
    see_more['url'] = new_url
    context['see_more_url'][agg_name] = see_more


def create_amount_aggregate(json_query):
    json_query["aggs"]["amountAwardedFixed"] = {"range": {"field": "amountAwarded", "ranges": FIXED_AMOUNT_RANGES}}


def get_amount_facet_fixed(request, context, original_json_query):
    json_query = copy.deepcopy(original_json_query)
    json_query["aggs"]["amountAwardedFixed"] = {"range": {"field": "amountAwarded", "ranges": FIXED_AMOUNT_RANGES}}
    try:
        current_filter = json_query["query"]["bool"]["filter"][2]["bool"]["should"]
    except KeyError:
        json_query["query"]["bool"]["filter"] = copy.deepcopy(BASIC_FILTER)
        current_filter = json_query["query"]["bool"]["filter"][2]["bool"]["should"]

    main_results = context["results"]

    json_query["query"]["bool"]["filter"][2]["bool"]["should"] = []

    existing_currency, current_currency = context['existing_currency'], context['current_currency']

    if current_currency and not existing_currency:
        json_query["query"]["bool"]["filter"][2]["bool"]["must"] = {"term": {"currency": current_currency}}

    json_query["query"]["bool"]["filter"][2]["bool"]["minimum_should_match"] = 0
    results = get_results(json_query)

    input_range = json_query["query"]["bool"]["filter"][3]["bool"]["should"]["range"]["amountAwarded"]
    new_filter = copy.deepcopy(current_filter)
    if new_filter or input_range:
        new_json_query = copy.deepcopy(original_json_query)
        new_json_query["query"]["bool"]["filter"][2]["bool"]["should"] = []
        new_json_query["query"]["bool"]["filter"][2]["bool"]["must"] = {}
        new_json_query["query"]["bool"]["filter"][3]["bool"]["should"]["range"]["amountAwarded"] = {}
        new_json_query["query"]["bool"]["filter"][3]["bool"]["must"] = {}
        results["aggregations"]["amountAwardedFixed"]["clear_url"] = request.path + '?' + create_parameters_from_json_query(new_json_query)

    for bucket in results["aggregations"]["amountAwardedFixed"]['buckets']:
        new_json_query = copy.deepcopy(original_json_query)
        new_filter = []
        new_range = {"gte": bucket["from"]}
        to_ = bucket.get("to")
        if to_:
            new_range["lt"] = to_
        for filter in current_filter:
            if filter["range"]["amountAwarded"] == new_range:
                bucket["selected"] = True
            else:
                new_filter.append(filter)
        if not bucket.get("selected"):
            new_filter.append({"range": {"amountAwarded": new_range}})

        new_json_query["query"]["bool"]["filter"][2]["bool"]["should"] = new_filter
        if not new_filter:
            new_json_query["query"]["bool"]["filter"][2]["bool"]["must"] = {}
        elif not existing_currency and current_currency:
            new_json_query["query"]["bool"]["filter"][2]["bool"]["must"] = {"term": {"currency": current_currency}}

        bucket["url"] = request.path + '?' + create_parameters_from_json_query(new_json_query)

        if bucket.get("selected"):
            display_value = "{}{:,}".format(utils.currency_prefix(current_currency), int(bucket["from"]))
            if to_:
                display_value = str(display_value) + " - " + "{}{:,}".format(utils.currency_prefix(current_currency), int(to_))
            else:
                display_value = str(display_value) + "+"

            context["selected_facets"]["Amounts"].append({"url": bucket["url"], "display_value": display_value})

    if input_range:
        new_json_query = copy.deepcopy(original_json_query)
        lte, gte = input_range.get('lte'), input_range.get('gte')
        if not gte:
            gte = 0
        display_value = "{}{:,}".format(utils.currency_prefix(current_currency), int(gte))
        if lte:
            display_value = str(display_value) + " - " + "{}{:,}".format(utils.currency_prefix(current_currency), int(lte))
        else:
            display_value = str(display_value) + "+"
        new_json_query["query"]["bool"]["filter"][3]["bool"]["should"]["range"]["amountAwarded"] = {}
        new_json_query["query"]["bool"]["filter"][3]["bool"]["must"] = {}

        context["selected_facets"]["Amounts"].append({"url": request.path + '?' + create_parameters_from_json_query(new_json_query), "display_value": display_value})

    main_results["aggregations"]["amountAwardedFixed"] = results["aggregations"]["amountAwardedFixed"]


def create_date_aggregate(json_query):
    json_query["aggs"]["awardYear"] = {"date_histogram": {"field": "awardDate", "format": "yyyy", "interval": "year", "order": {"_key": "desc"}}}


def get_date_facets(request, context, json_query):
    json_query = copy.deepcopy(json_query)
    try:
        current_filter = json_query["query"]["bool"]["filter"][4]["bool"]["should"]
    except KeyError:
        json_query["query"]["bool"]["filter"] = copy.deepcopy(BASIC_FILTER)
        current_filter = json_query["query"]["bool"]["filter"][4]["bool"]["should"]
    main_results = context["results"]
    if current_filter:
        json_query["query"]["bool"]["filter"][4]["bool"]["should"] = []
        create_date_aggregate(json_query)
        results = get_results(json_query)
    else:
        results = context["results"]

    for bucket in results['aggregations']['awardYear']['buckets']:
        range = {'format': 'year'}
        value = bucket.get("key_as_string")
        range["gte"] = value + "||/y"
        range["lte"] = value + "||/y"

        filter_values = [filter["range"]['awardDate'] for filter in current_filter]

        if range in filter_values:
            bucket["selected"] = True
            range['format'] = 'year'
            filter_values.remove(range)
        else:
            range['format'] = 'year'
            filter_values.append(range)

        new_filter = [{"range": {"awardDate": value}} for value in filter_values]
        json_query["query"]["bool"]["filter"][4]["bool"]["should"] = new_filter
        bucket["url"] = request.path + '?' + create_parameters_from_json_query(json_query)

        if bucket.get("selected"):
            context["selected_facets"]["Award Year"].append({"url": bucket["url"], "display_value": value})

    if current_filter:
        json_query["query"]["bool"]["filter"][4]["bool"]["should"] = []
        results['aggregations']["awardYear"]['clear_url'] = request.path + '?' + create_parameters_from_json_query(json_query)
    main_results['aggregations']["awardYear"] = results['aggregations']["awardYear"]


def get_terms_facets(request, context, json_query, field, aggregate, bool_index, display_name, is_json=False):

    json_query = copy.deepcopy(json_query)
    try:
        if "must_not" in json_query["query"]["bool"]["filter"][bool_index]["bool"]:
            bool_condition = "must_not"
        else:
            bool_condition = "should"

        current_filter = json_query["query"]["bool"]["filter"][bool_index]["bool"].get(bool_condition, [])
    except KeyError:
        json_query["query"]["bool"]["filter"] = copy.deepcopy(BASIC_FILTER)
        current_filter = json_query["query"]["bool"]["filter"][bool_index]["bool"].get(bool_condition, [])

    main_results = context["results"]
    if current_filter:
        json_query["query"]["bool"]["filter"][bool_index]["bool"][bool_condition] = []
        results = get_results(json_query)
    else:
        results = context["results"]

    if bool_condition == 'must_not':
        display_name = 'Excluded ' + display_name

    for filter in current_filter:
        new_filter = [x for x in current_filter if x != filter]
        json_query["query"]["bool"]["filter"][bool_index]["bool"][bool_condition] = new_filter
        display_value = filter["term"][field]
        if is_json:
            display_value = json.loads(display_value)[0]
        context["selected_facets"][display_name].append(
            {"url": request.path + '?' + create_parameters_from_json_query(json_query),
             "display_value": display_value}
        )

    for bucket in results['aggregations'][aggregate]['buckets']:
        facet_value = bucket['key']
        filter_values = [filter["term"][field] for filter in current_filter]
        if facet_value in filter_values:
            bucket["selected"] = True
            filter_values.remove(facet_value)
        else:
            filter_values.append(facet_value)

        new_filter = [{"term": {field: value}} for value in filter_values]
        json_query["query"]["bool"]["filter"][bool_index]["bool"][bool_condition] = new_filter
        bucket["url"] = request.path + '?' + create_parameters_from_json_query(json_query)
    if current_filter:
        json_query["query"]["bool"]["filter"][bool_index]["bool"][bool_condition] = []
        results['aggregations'][aggregate]['clear_url'] = request.path + '?' + create_parameters_from_json_query(json_query)
        results['aggregations'][aggregate]["exclude"] = True if bool_condition == "must_not" else False

    main_results['aggregations'][aggregate] = results['aggregations'][aggregate]


def get_clear_all(request, context, json_query):
    json_query = copy.deepcopy(json_query)
    try:
        current_filter = json_query["query"]["bool"]["filter"]
    except KeyError:
        json_query["query"]["bool"]["filter"] = copy.deepcopy(BASIC_FILTER)
        current_filter = json_query["query"]["bool"]["filter"]

    if current_filter != BASIC_FILTER:
        json_query["query"]["bool"]["filter"] = copy.deepcopy(BASIC_FILTER)
        context["results"]["clear_all_facet_url"] = request.path + '?' + create_parameters_from_json_query(json_query)


def totals_query():
    query = {"query": {"match_all": {}},
             "aggs": {
                 "recipient_count": {"cardinality": {"field": "recipientOrganization.id", "precision_threshold": 40000}},
                 "funder_count": {"cardinality": {"field": "fundingOrganization.id", "precision_threshold": 40000}},
        }
    }

    return get_results(query)


def home(request):
    results = totals_query()

    context = {}
    context['results'] = results

    return render(request, "home.html", context=context)


def add_advanced_search_information_in_context(context):
    """
    When a user's search query is 2+ words without quotes, with 'and' or 'or'.
    Advanced search information is displayed.
    """
    text_query = context.get('text_query').lower()
    if text_query is not None and len(text_query) > 1:
        if re.search(r'\b and \b', text_query):
            context["advanced_search_info"] = 'The AND keyword (not case-sensitive) means that results must have ' \
                'both words present. If you\'re looking for a phrase that has the word "and" in it, put quotes ' \
                'around the phrase (e.g. "fees and costs").'
        elif re.search(r'\b or \b', text_query):
            context["advanced_search_info"] = 'The OR keyword (not case-sensitive) means that results must have one ' \
                'of the words present. This is the default. If you\'re looking for a phrase that has the word "or" ' \
                'in (e.g. "children or adults"), put quotes around it.'
        elif ' ' in context.get('text_query') \
                and not (text_query.startswith("'") and text_query.endswith("'")) \
                and not (text_query.startswith('"') and text_query.endswith('"')):
            context["advanced_search_info"] = 'If you\'re looking for a specific phrase, put quotes around it to ' \
                'refine your search. e.g. "youth clubs".'
    return context


def term_facet_from_parameters(request, json_query, field_name, param_name, bool_index, field, is_json=False):
    new_filter = []

    if is_json:
        query_filter = []
        for value in request.GET.getlist(param_name):
            query_filter.append({"term": {param_name + '.id': value}})

        if query_filter:
            query = {
                "query": {
                    "bool": {"should": query_filter}
                },
                "aggs": {
                    param_name: {"terms": {"field": field_name, "size": len(query_filter)}}
                }
            }

            results = get_results(query, 1)

            for bucket in results['aggregations'][param_name]['buckets']:
                new_filter.append({"term": {field_name: bucket['key']}})

    else:
        for value in request.GET.getlist(param_name):
            new_filter.append({"term": {field_name: value}})

    if request.GET.get("exclude_" + param_name):
        json_query["query"]["bool"]["filter"][bool_index]["bool"].pop("should", None)
        json_query["query"]["bool"]["filter"][bool_index]["bool"]["must_not"] = new_filter
    else:
        json_query["query"]["bool"]["filter"][bool_index]["bool"]["should"] = new_filter


def amount_facet_from_parameters(request, json_query):
    new_filter = []
    for value in request.GET.getlist('amountAwarded'):
        for item in FIXED_AMOUNT_RANGES:
            if value == str(item['from']):
                new_range = {"gte": item["from"]}
                to_ = item.get("to")
                if to_:
                    new_range["lt"] = to_
                new_filter.append({"range": {"amountAwarded": new_range}})

    json_query["query"]["bool"]["filter"][2]["bool"]["should"] = new_filter


def date_facet_from_parameters(request, json_query):
    new_filter = []
    for value in request.GET.getlist("awardDate"):
        new_filter.append(
            {"range": {
                "awardDate": {"format": "year",
                              "gte": value + "||/y",
                              "lte": value + "||/y"}
            }
            }
        )

    json_query["query"]["bool"]["filter"][4]["bool"]["should"] = new_filter


def term_facet_size_from_parameters(request, json_query):
    try:
        aggs = json_query["aggs"]
    except KeyError:
        aggs = BASIC_QUERY['aggs']

    for agg_name, agg in aggs.items():
        if "terms" not in agg or agg["terms"]["size"] == DROPDOWN_SIZE:
            continue

        more = request.GET.get(agg_name + 'More')
        if more:
            agg["terms"]["size"] = MORE_SIZE
        else:
            agg["terms"]["size"] = LESS_SIZE


def non_term_facet_size_from_parameters(request, json_query, agg_name):

    more = request.GET.get(agg_name + 'More')
    if more:
        json_query['extra_context'][agg_name + '_facet_size'] = MORE_SIZE
    else:
        json_query['extra_context'][agg_name + '_facet_size'] = LESS_SIZE


def create_json_query_from_parameters(request):
    """ Transforms the URL GET parameters of the request into an object (json_query) that is to be used by elasticsearch  """

    json_query = copy.deepcopy(BASIC_QUERY)
    json_query["query"]["bool"]["must"]["query_string"]["query"] = request.GET.get('query', '*')
    json_query["query"]["bool"]["must"]["query_string"]["default_field"] = request.GET.get('default_field', '*')

    sort_order = request.GET.get('sort', '').split()
    if sort_order and len(sort_order) == 2:
        sort = {sort_order[0]: {"order": sort_order[1]}}
        json_query["sort"] = sort

    amount_filter = {}
    min_amount = request.GET.get('min_amount')
    if min_amount:
        amount_filter['gte'] = min_amount
    max_amount = request.GET.get('max_amount')
    if max_amount:
        amount_filter['lte'] = max_amount
    json_query["query"]["bool"]["filter"][3]["bool"]["should"]["range"]["amountAwarded"] = amount_filter

    for term_facet in TERM_FACETS:
        term_facet_from_parameters(request, json_query, term_facet.field_name, term_facet.param_name,
                                   term_facet.filter_index, term_facet.display_name, term_facet.is_json)

    amount_facet_from_parameters(request, json_query)
    date_facet_from_parameters(request, json_query)
    term_facet_size_from_parameters(request, json_query)

    non_term_facet_size_from_parameters(request, json_query, 'awardYear')
    non_term_facet_size_from_parameters(request, json_query, 'amountAwardedFixed')

    return json_query


def term_parameters_from_json_query(parameters, json_query, field_name, param_name, bool_index, field, is_json=False):
    values = []
    if "must_not" in json_query["query"]["bool"]["filter"][bool_index]["bool"]:
        filters = json_query["query"]["bool"]["filter"][bool_index]["bool"]["must_not"]
        must_not = True
    else:
        filters = json_query["query"]["bool"]["filter"][bool_index]["bool"]["should"]
        must_not = False

    for filter in filters:
        if is_json:
            values.append(json.loads(filter['term'][field_name])[1])
        else:
            values.append(filter['term'][field_name])
    parameters[param_name] = values
    if must_not:
        parameters["exclude_" + param_name] = "true"


def amount_parameters_from_json_query(parameters, json_query):
    values = []
    for filter in json_query["query"]["bool"]["filter"][2]["bool"]["should"]:
        values.append(int(filter['range']['amountAwarded']['gte']))
    parameters['amountAwarded'] = values


def date_parameters_from_json_query(parameters, json_query):
    values = []
    for filter in json_query["query"]["bool"]["filter"][4]["bool"]["should"]:
        values.append(filter['range']['awardDate']['gte'][:-4])  # remove the ||/y from the end
    parameters['awardDate'] = values


def term_facet_size_from_json_query(parameters, json_query):

    try:
        aggs = json_query["aggs"]
    except KeyError:
        aggs = BASIC_QUERY['aggs']

    for agg_name, agg in aggs.items():
        if "terms" not in agg:
            continue
        if agg["terms"]["size"] == MORE_SIZE:
            parameters[agg_name + 'More'] = ['true']


def non_term_facet_size_from_json_query(parameters, json_query, agg_name):

    if json_query['extra_context'][agg_name + '_facet_size'] == MORE_SIZE:
        parameters[agg_name + 'More'] = ['true']


def create_parameters_from_json_query(json_query, **extra_parameters):
    """ Transforms json_query (the query that is passed to elasticsearch) to URL GET parameters """

    parameters = {}

    parameters['query'] = [json_query["query"]["bool"]["must"]["query_string"]["query"]]
    parameters['default_field'] = [json_query["query"]["bool"]["must"]["query_string"]["default_field"]]

    sort_key = list(json_query["sort"].keys())[0]
    parameters['sort'] = [sort_key + ' ' + json_query['sort'][sort_key]['order']]

    min_amount = json_query["query"]["bool"]["filter"][3]["bool"]["should"]["range"]["amountAwarded"].get('gte')
    if min_amount:
        parameters['min_amount'] = [str(min_amount)]
    max_amount = json_query["query"]["bool"]["filter"][3]["bool"]["should"]["range"]["amountAwarded"].get('lte')
    if max_amount:
        parameters['max_amount'] = [str(max_amount)]

    for term_facet in TERM_FACETS:
        term_parameters_from_json_query(parameters, json_query, term_facet.field_name, term_facet.param_name,
                                        term_facet.filter_index, term_facet.display_name, term_facet.is_json)

    amount_parameters_from_json_query(parameters, json_query)
    date_parameters_from_json_query(parameters, json_query)
    term_facet_size_from_json_query(parameters, json_query)

    non_term_facet_size_from_json_query(parameters, json_query, 'awardYear')
    non_term_facet_size_from_json_query(parameters, json_query, 'amountAwardedFixed')

    parameter_list = []

    for parameter, list_value in parameters.items():
        for value in list_value:
            parameter_list.append((parameter, value))
    for parameter, value in extra_parameters.items():
        parameter_list.append((parameter, value))

    return urlencode(parameter_list)


def search(request):
    [result_format, results_size] = get_request_type_and_size(request)

    context = {}

    query = request.GET.urlencode()
    if query:
        context['query_string'] = query
    else:
        context['query_string'] = ""

    # URL query backwards compatibility v2
    # https://github.com/ThreeSixtyGiving/grantnav/issues/571
    # Allows old json_query get method
    json_query = request.GET.get('json_query') or ""
    try:
        json_query = json.loads(json_query)
    except ValueError:
        json_query = {}
    # End URL query backwards compatibility v2

    if not json_query:
        json_query = create_json_query_from_parameters(request)

    default_field = request.GET.get('default_field')

    # URL query backwards compatibility v1
    # https://github.com/ThreeSixtyGiving/grantnav/issues/541
    # Subsitute _all for * in default_field
    try:
        if "_all" in json_query["query"]["bool"]["must"]["query_string"]["default_field"]:
            json_query["query"]["bool"]["must"]["query_string"]["default_field"] = "*"
            return redirect(request.path + '?' + create_parameters_from_json_query(json_query))
    except KeyError:
        pass
    # End URL query backwards compatibility v1

    text_query = request.GET.get('text_query')
    if text_query is not None:
        if not text_query:
            text_query = '*'
        try:
            json_query["query"]["bool"]["must"]["query_string"]["query"] = text_query
        except KeyError:
            json_query = copy.deepcopy(BASIC_QUERY)
            json_query["query"]["bool"]["must"]["query_string"]["query"] = text_query

        if default_field:
            json_query["query"]["bool"]["must"]["query_string"]["default_field"] = default_field
        return redirect(request.path + '?' + create_parameters_from_json_query(json_query))

    sort_order = request.GET.get('sort', '').split()
    if sort_order and len(sort_order) == 2:
        new_sort = {sort_order[0]: {"order": sort_order[1]}}
        old_sort = json_query["sort"]
        if new_sort != old_sort:
            json_query["sort"] = new_sort
            return redirect(request.path + '?' + create_parameters_from_json_query(json_query))

    results = None
    if json_query:
        try:
            page = int(request.GET.get('page', 1))
            if page < 1:
                page = 1
        except ValueError:
            page = 1

        try:
            context['text_query'] = json_query["query"]["bool"]["must"]["query_string"]["query"]
            context['default_field'] = json_query["query"]["bool"]["must"]["query_string"]["default_field"]
        except KeyError:
            json_query = copy.deepcopy(BASIC_QUERY)
            json_query["query"]["bool"]["must"]["query_string"]["query"] = ''
            context['text_query'] = ''
            if default_field:
                json_query["query"]["bool"]["must"]["query_string"]["default_field"] = default_field
            context['default_field'] = json_query["query"]["bool"]["must"]["query_string"]["default_field"]

        if result_format == "csv":
            return grants_csv_paged(json_query)
        elif result_format == "json":
            return grants_json_paged(json_query)

        if context['text_query'] == '*':
            context['text_query'] = ''

        try:
            create_amount_aggregate(json_query)
            create_date_aggregate(json_query)

            json_query['aggs'].update(SEARCH_SUMMARY_AGGREGATES)

            results = get_results(json_query, results_size, (page - 1) * SIZE)
            for key in SEARCH_SUMMARY_AGGREGATES:
                json_query["aggs"].pop(key)

            json_query["aggs"].pop("awardYear")
            json_query["aggs"].pop("amountAwardedFixed")
        except elasticsearch.exceptions.RequestError as e:
            if e.error == 'search_phase_execution_exception':
                context['search_error'] = True
                return render(request, "search.html", context=context)
            raise

        for hit in results['hits']['hits']:
            hit['source'] = hit['_source']
        context['results'] = results
        context['json_query'] = json.dumps(json_query)
        context['query'] = json_query

        current_currency = None
        existing_currency = None
        if json_query["query"]["bool"]["filter"][2]["bool"]["must"]:
            existing_currency = json_query["query"]["bool"]["filter"][2]["bool"]["must"]["term"]["currency"]
            current_currency = existing_currency
        currency_facets = context["results"]['aggregations']['currency']['buckets']
        if not current_currency and currency_facets:
            current_currency = currency_facets[0]['key']
        context['existing_currency'] = existing_currency
        context['current_currency'] = current_currency

        min_amount = request.GET.get('new_min_amount')
        max_amount = request.GET.get('new_max_amount')
        if min_amount or max_amount:
            new_filter = {}
            if min_amount:
                try:
                    new_filter['gte'] = int(min_amount)
                except ValueError:
                    pass
            if max_amount:
                try:
                    new_filter['lte'] = int(max_amount)
                except ValueError:
                    pass
            json_query["query"]["bool"]["filter"][3]["bool"]["should"]["range"]["amountAwarded"] = new_filter
            json_query["query"]["bool"]["filter"][3]["bool"]["must"] = {"term": {"currency": current_currency}}
            return redirect(request.path + '?' + create_parameters_from_json_query(json_query))

        context['selected_facets'] = collections.defaultdict(list)
        get_clear_all(request, context, json_query)

        for term_facet in TERM_FACETS:
            get_terms_facets(request, context, json_query, term_facet.field_name, term_facet.param_name,
                             term_facet.filter_index, term_facet.display_name, term_facet.is_json)

        get_amount_facet_fixed(request, context, json_query)
        get_date_facets(request, context, json_query)
        get_terms_facet_size(request, context, json_query, page)
        get_non_terms_facet_size(request, context, json_query, page, 'awardYear')
        get_non_terms_facet_size(request, context, json_query, page, 'amountAwardedFixed')

        get_pagination(request, context, page)

        context['selected_facets'] = dict(context['selected_facets'])

        add_advanced_search_information_in_context(context)

        return render(request, "search.html", context=context)


def grant(request, grant_id):
    query = {"query": {"bool": {"filter":
                [{"term": {"id": grant_id}}]
    }}}
    # -1 Show all results for this grant id
    results = get_results(query, -1)
    if results['hits']['total']['value'] == 0:
        raise Http404
    context = {}
    for hit in results['hits']['hits']:
        hit['source'] = hit['_source']
    context['grants'] = results['hits']['hits']
    return render(request, "grant.html", context=context)


def funder(request, funder_id):
    """
    Returns grant data based on the funder_id.
    Used to create summary information to complement grant datatables unless csv or json specified
    in which case this provides the method of downloading the data.
    """

    [result_format, results_size] = get_request_type_and_size(request)

    if result_format != "html":
        funder_id = re.match(r'(.*)\.\w*$', funder_id).group(1)

    query = {"query": {"bool": {"filter":
                [{"term": {"fundingOrganization.id": funder_id}}]}},
            "aggs": {
                "recipient_orgs": {"cardinality": {"field": "recipientOrganization.id", "precision_threshold": 40000}},
                "filenames": {"terms": {"field": "filename", "size": 10}},
                "currency_stats": {"terms": {"field": "currency"}, "aggs": {"amount_stats": {"stats": {"field": "amountAwarded"}}}},
                "min_date": {"min": {"field": "awardDate"}},
                "max_date": {"max": {"field": "awardDate"}},
                "recipients": {"terms": {"field": "recipientOrganization.id_and_name", "size": 10},
                               "aggs": {"recipient_stats": {"stats": {"field": "amountAwarded"}}}}
        }
    }
    if result_format == "csv":
        return grants_csv_paged(query)
    elif result_format == "json":
        return grants_json_paged(query)

    results = get_results(query, results_size)

    if results['hits']['total']['value'] == 0:
        raise Http404
    context = {}
    context['results'] = results

    funder = results['hits']['hits'][0]["_source"]["fundingOrganization"][0].copy()
    # funder name for this case to come from same place as Filters.
    funder['name'] = json.loads(funder['id_and_name'])[0]

    context['funder'] = funder
    try:
        context['publisher'] = provenance.by_identifier[provenance.identifier_from_filename(results['aggregations']['filenames']['buckets'][0]['key'])]['publisher']
    except KeyError:
        pass

    return render(request, "funder.html", context=context)


def funder_recipients_datatables(request):
    """
     Ajax endpoint for recipients datatables.
     Provides download endpoint for datatable data as csv or json if csv or json extension present.
    """
    # Make 100k the default max length. Overrideable by setting ?length= parameter
    MAX_DEFAULT_FUNDER_RECIPIENTS_LENGTH = 100000

    match = re.search(r'\.(\w+)$', request.path)
    if match:
        result_format = match.group(1)
    else:
        result_format = "ajax"

    order = ["_term", "recipient_stats.count", "recipient_stats.sum", "recipient_stats.avg", "recipient_stats.max", "recipient_stats.min"]

    length = int(request.GET.get('length', MAX_DEFAULT_FUNDER_RECIPIENTS_LENGTH))

    if result_format == "ajax":
        start = int(request.GET['start'])
        order_field = order[int(request.GET['order[0][column]'])]
        search_value = request.GET['search[value]']
        order_dir = request.GET['order[0][dir]']
    else:
        start = 0
        order_field = order[0]
        search_value = ""
        order_dir = "desc"

    funder_id = request.GET.get('funder_id')
    if funder_id:
        filter = {"term": {"fundingOrganization.id": funder_id}}
    else:
        filter = {}

    currency = request.GET.get('currency')
    if not currency:
        currency = 'GBP'

    query = {"query": {
             "bool": {
                 "filter":
                     [filter, {"term": {"currency": currency}}],
                 "must":
                     {"match": {"recipientOrganization.name": {"query": search_value, "operator": "and"}}},
                 },
             },
             "aggs": {
                 "recipient_count": {"cardinality": {"field": "recipientOrganization.id", "precision_threshold": 40000}},
                 "recipient_stats":
                     {"terms": {"field": "recipientOrganization.id_and_name", "size": start + length, "shard_size": (start + length) * 10,
                                "order": {order_field: order_dir}},
                      "aggs": {"recipient_stats": {"stats": {"field": "amountAwarded"}}}}
        }
    }
    if not search_value:
        query["query"]["bool"].pop("must")

    results = get_results(query, 1)
    result_list = []

    for result in results["aggregations"]["recipient_stats"]["buckets"][-length:]:
        stats = result["recipient_stats"]
        for key in list(stats):
            if key != 'count':
                if result_format == "ajax":
                    stats[key] = "{}{:,.0f}".format(utils.currency_prefix(currency), int(stats[key]))
                else:
                    stats[key] = "{:.0f}".format(int(stats[key]))
        org_name, org_id = json.loads(result["key"])
        stats["org_name"] = org_name
        stats["org_id"] = org_id
        result_list.append(stats)

    if result_format == "ajax":
        return JsonResponse(
            {'data': result_list,
             'draw': request.GET['draw'],
             'recordsTotal': results["aggregations"]["recipient_count"]["value"],
             'recordsFiltered': results["aggregations"]["recipient_count"]["value"]}
        )
    elif result_format == "csv":
        return orgs_csv_paged(result_list, "recipient")
    elif result_format == "json":
        response = HttpResponse(json.dumps({'data': result_list}), content_type='application/json')
        response['Content-Disposition'] = 'attachment; filename="grantnav-{0}.json"'.format(datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
        return response


def funders_datatables(request):
    """
     Ajax endpoint for funders datatables.
     Provides download endpoint for datatable data as csv or json if csv or json extension present.
    """
    match = re.search(r'\.(\w+)$', request.path)
    # Make 100k the default max length. Overrideable by setting ?length= parameter
    MAX_DEFAULT_FUNDERS_LENGTH = 100000

    if match:
        result_format = match.group(1)
    else:
        result_format = "ajax"

    order = ["_term", "funder_stats.count", "funder_stats.sum", "funder_stats.avg", "funder_stats.max", "funder_stats.min"]

    length = int(request.GET.get('length', MAX_DEFAULT_FUNDERS_LENGTH))

    if result_format == "ajax":
        start = int(request.GET['start'])
        order_field = order[int(request.GET['order[0][column]'])]
        search_value = request.GET['search[value]']
        order_dir = request.GET['order[0][dir]']
    else:
        start = 0
        order_field = order[0]
        search_value = ""
        order_dir = "desc"

    recipient_id = request.GET.get('recipient_id')
    if recipient_id:
        filter = {"term": {"recipientOrganization.id": recipient_id}}
    else:
        filter = {}

    query = {"query": {
             "bool": {
                 "filter":
                     [filter, {"term": {"currency": "GBP"}}],
                 "must":
                     {"match": {"fundingOrganization.name": {"query": search_value, "operator": "and"}}},
                 },
             },
             "aggs": {
                 "funder_count": {"cardinality": {"field": "fundingOrganization.id", "precision_threshold": 40000}},
                 "funder_stats":
                     {"terms": {"field": "fundingOrganization.id_and_name", "size": start + length, "shard_size": (start + length) * 10,
                                "order": {order_field: order_dir}},
                      "aggs": {"funder_stats": {"stats": {"field": "amountAwarded"}}}}
        }
    }
    if not search_value:
        query["query"]["bool"].pop("must")

    results = get_results(query, 1)
    result_list = []

    for result in results["aggregations"]["funder_stats"]["buckets"][-length:]:
        stats = result["funder_stats"]
        for key in list(stats):
            if result_format == "ajax":
                if "count" in key:
                    stats[key] = "{:,}".format(stats[key])
                else:
                    stats[key] = "£ {:,.0f}".format(int(stats[key]))
            else:
                if "count" not in key:
                    stats[key] = "{:.0f}".format(int(stats[key]))

        org_name, org_id = json.loads(result["key"])
        stats["org_name"] = org_name
        stats["org_id"] = org_id
        result_list.append(stats)

    if result_format == "ajax":
        return JsonResponse(
            {'data': result_list,
             'draw': request.GET['draw'],
             'recordsTotal': results["aggregations"]["funder_count"]["value"],
             'recordsFiltered': results["aggregations"]["funder_count"]["value"]}
        )
    elif result_format == "csv":
        return orgs_csv_paged(result_list, "funder")
    elif result_format == "json":
        response = HttpResponse(json.dumps({'data': result_list}), content_type='application/json')
        response['Content-Disposition'] = 'attachment; filename="grantnav-{0}.json"'.format(datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
        return response


grant_datatables_metadata = {
    "funder": {
        "term": "fundingOrganization.id",
        "order": ["awardDate", "amountAwarded", "recipientOrganization.id_and_name", "title", "description"],
    },
    "recipient": {
        "term": "recipientOrganization.id",
        "order": ["awardDate", "amountAwarded", "fundingOrganization.id_and_name", "title", "description"],
    },
    "recipientRegionName": {
        "term": "additional_data.recipientRegionName",
        "order": ["awardDate", "amountAwarded", "fundingOrganization.id_and_name", "recipientOrganization.id_and_name", "title", "description"],
    },
    "recipientDistrictName": {
        "term": "additional_data.recipientDistrictName",
        "order": ["awardDate", "amountAwarded", "fundingOrganization.id_and_name", "recipientOrganization.id_and_name", "title", "description"],
    }
}


def grants_datatables(request):
    """" Ajax call for the various grants datatables """

    # Fetch the keys from the grant_datatables_metadata from the GET parameters
    # fill in the values to create the es query.
    for field, metadata in grant_datatables_metadata.items():
        if field in request.GET:
            value = request.GET[field]
            order = metadata["order"]
            term = metadata["term"]
            break

    order_field = order[int(request.GET['order[0][column]'])]
    search_value = request.GET['search[value]']
    order_dir = request.GET['order[0][dir]']
    start = int(request.GET['start'])
    length = int(request.GET['length'])
    query = {"query": {
             "bool": {
                 "filter":
                     {"term": {term: value}},
                 "must":
                     {"query_string": {"query": search_value, "default_operator": "and"}},
                 },
             },
             "sort": [{order_field: order_dir}]}
    if not search_value:
        query["query"]["bool"].pop("must")

    try:
        results = get_results(query, length, start)
    except elasticsearch.exceptions.RequestError as e:
        if e.error == 'search_phase_execution_exception':
            results = {"hits": {"total": {"value": 0}, "hits": []}}

    result_list = []
    for result in results["hits"]["hits"]:
        grant = result["_source"]
        try:
            grant["awardDate"] = date_parser.parse(grant["awardDate"]).strftime("%d %b %Y")
        except ValueError:
            pass
        try:
            grant["amountAwarded"] = utils.currency_prefix(grant.get("currency")) + "{:,.0f}".format(grant["amountAwarded"])
        except ValueError:
            pass
        grant["description"] = grant.get("description", "")
        grant["title"] = grant.get("title", "")
        result_list.append(grant)

    return JsonResponse(
        {'data': result_list,
         'draw': request.GET['draw'],
         'recordsTotal': results["hits"]["total"]['value'],
         'recordsFiltered': results["hits"]["total"]['value']}
    )


def recipient(request, recipient_id):
    """
    Returns grant data based on the recipient_id.
    Used to create summary information to complement grant datatables unless csv or json specified
    in which case this provides the method of downloading the data.
    """

    [result_format, results_size] = get_request_type_and_size(request)
    if result_format != "html":
        recipient_id = re.match(r'(.*)\.\w*$', recipient_id).group(1)

    query = {"query": {"bool": {"filter":
                 [{"term": {"recipientOrganization.id": recipient_id}}]}},
             "aggs": {
                 "funder_orgs": {"cardinality": {"field": "fundingOrganization.id"}},
                 "currency_stats": {"terms": {"field": "currency"}, "aggs": {"amount_stats": {"stats": {"field": "amountAwarded"}}}},
                 "min_date": {"min": {"field": "awardDate"}},
                 "max_date": {"max": {"field": "awardDate"}},
                 "currencies": {"terms": {"field": "currency", "size": 1000}},  # less that 1000 currencies with code.
                 "funders": {"terms": {"field": "fundingOrganization.id_and_name", "size": 10},
                             "aggs": {"funder_stats": {"stats": {"field": "amountAwarded"}}}}
                 }}

    if result_format == "csv":
        return grants_csv_paged(query)
    elif result_format == "json":
        return grants_json_paged(query)

    results = get_results(query, results_size)

    if results['hits']['total']['value'] == 0:
        raise Http404
    context = {}
    context['results'] = results
    context['recipient'] = results['hits']['hits'][0]["_source"]["recipientOrganization"][0]

    return render(request, "recipient.html", context=context)


def recipients(request):
    return render(request, "recipients.html")


def funders(request):
    return render(request, "funders.html")


def region(request, region):
    """
    Returns grant data based on the region.
    Used to create summary information to complement grant datatables unless csv or json specified
    in which case this provides the method of downloading the data.
    """

    [result_format, results_size] = get_request_type_and_size(request)
    if result_format != "html":
        region = re.match(r'(.*)\.\w*$', region).group(1)

    query = {"query": {"bool": {"filter":
                [{"term": {"recipientRegionName": region}}]}},
            "aggs": {
                "recipient_orgs": {"cardinality": {"field": "recipientOrganization.id", "precision_threshold": 40000}},
                "funding_orgs": {"cardinality": {"field": "fundingOrganization.id", "precision_threshold": 40000}},
                "currency_stats": {"terms": {"field": "currency"}, "aggs": {"amount_stats": {"stats": {"field": "amountAwarded"}}}},
                "min_date": {"min": {"field": "awardDate"}},
                "max_date": {"max": {"field": "awardDate"}},
        }
    }

    if result_format == "csv":
        return grants_csv_paged(query)
    elif result_format == "json":
        return grants_json_paged(query)

    results = get_results(query, results_size)

    if results['hits']['total']['value'] == 0:
        raise Http404
    context = {}
    context['results'] = results
    context['region'] = region

    return render(request, "region.html", context=context)


def district(request, district):
    """
    Returns grant data based on the district.
    Used to create summary information to complement grant datatables unless csv or json specified
    in which case this provides the method of downloading the data.
    """

    [result_format, results_size] = get_request_type_and_size(request)
    if result_format != "html":
        district = re.match(r'(.*)\.\w*$', district).group(1)

    query = {"query": {"bool": {"filter":
                [{"term": {"additional_data.recipientDistrictName": district}}]}},
            "aggs": {
                "recipient_orgs": {"cardinality": {"field": "recipientOrganization.id", "precision_threshold": 40000}},
                "funding_orgs": {"cardinality": {"field": "fundingOrganization.id", "precision_threshold": 40000}},
                "currency_stats": {"terms": {"field": "currency"}, "aggs": {"amount_stats": {"stats": {"field": "amountAwarded"}}}},
                "min_date": {"min": {"field": "awardDate"}},
                "max_date": {"max": {"field": "awardDate"}},
        }
    }

    if result_format == "csv":
        return grants_csv_paged(query)
    elif result_format == "json":
        return grants_json_paged(query)

    results = get_results(query, results_size)

    if results['hits']['total']['value'] == 0:
        raise Http404
    context = {}
    context['results'] = results
    context['district'] = district

    return render(request, "district.html", context=context)


def get_funders_for_datasets(datasets):
    es = get_es()

    for dataset in datasets:
        query = {"query": {"bool": {
            "filter": [{"term": {"filename": dataset['identifier'] + '.json'}}]}},
            "aggs": {
                "funders": {"terms": {"field": "fundingOrganization.id_and_name", "size": 10}}
            }
        }
        results = es.search(body=query, index=get_index(), size=0)
        dataset['funders'] = [json.loads(bucket['key']) for bucket in results['aggregations']['funders']['buckets']]


def publisher(request, publisher_id):
    publisher = provenance.by_publisher[publisher_id]
    get_funders_for_datasets(publisher['datasets'])

    if publisher_id not in provenance.by_publisher:
        raise Http404
    return render(request, "publisher.html", context={
        'publisher': publisher,
    })


def datasets(request):
    get_funders_for_datasets(provenance.datasets)
    return render(request, "datasets.html", context={
        'publishers': provenance.by_publisher.values(),
        'datasets': provenance.datasets,
    })


def api_grants(request):
    [result_format, results_size] = get_request_type_and_size(request)
    query = {"query": {"match_all": {}}}
    if result_format == "csv":
        return grants_csv_paged(query)
    elif result_format == "json":
        return grants_json_paged(query)
