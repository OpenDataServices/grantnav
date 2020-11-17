import os
import time

import pytest
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import Select

from dataload.import_to_elasticsearch import import_to_elasticsearch

prefix = 'https://raw.githubusercontent.com/OpenDataServices/grantnav-sampledata/560a8d9f21a069a9d51468850188f34ae72a0ec3/'


BROWSER = os.environ.get('BROWSER', 'ChromeHeadless')


@pytest.fixture(scope="module")
def browser(request):
    if BROWSER == 'ChromeHeadless':
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        browser = webdriver.Chrome(chrome_options=chrome_options)
    elif BROWSER == "Firefox":
        # Make downloads work
        profile = webdriver.FirefoxProfile()
        profile.set_preference("browser.download.folderList", 2)
        profile.set_preference("browser.download.manager.showWhenStarting", False)
        profile.set_preference("browser.download.dir", os.getcwd())
        profile.set_preference("browser.helperApps.neverAsk.saveToDisk", "application/json")
        browser = getattr(webdriver, BROWSER)(firefox_profile=profile)
    else:
        browser = getattr(webdriver, BROWSER)()
    browser.implicitly_wait(3)
    request.addfinalizer(lambda: browser.quit())
    return browser


@pytest.fixture(scope="module")
def server_url(request, live_server):
    if 'CUSTOM_SERVER_URL' in os.environ:
        return os.environ['CUSTOM_SERVER_URL']
    else:
        return live_server.url


@pytest.fixture(scope="module")
def dataload():
    import_to_elasticsearch([prefix + 'a002400000KeYdsAAF.json',
                             prefix + 'a002400000OiDBQAA3.xlsx',
                             prefix + 'a002400000G4KGJAA3.csv'], clean=True)
    #elastic search needs some time to commit its data
    time.sleep(2)


@pytest.fixture(scope="function")
def provenance_dataload(dataload, settings, tmpdir):
    local_data_json = tmpdir.join('data.json')
    local_data_json.write(requests.get(prefix + 'data.json').content)
    settings.PROVENANCE_JSON = local_data_json.strpath


def test_home(provenance_dataload, server_url, browser):
    browser.get(server_url)
    assert 'GrantNav' in browser.find_element_by_tag_name('body').text

    assert 'Cookies disclaimer' in browser.find_element_by_id('CookielawBanner').text
    browser.find_element_by_class_name("btn").click()
    browser.get(server_url)
    assert 'Cookies disclaimer' not in browser.find_element_by_tag_name('body').text
    assert '180Giving' in browser.find_element_by_tag_name('body').text
    assert '360Giving data standard' not in browser.find_element_by_tag_name('body').text


@pytest.mark.parametrize(('link_text'), [
    ('Forum'),
    ('Help'),
    ])
def test_navbar_links(provenance_dataload, server_url, browser, link_text):
    browser.get(server_url)
    browser.find_element_by_link_text(link_text)


def test_nav_menu_forum_link(provenance_dataload, server_url, browser):
    browser.get(server_url)
    browser.find_element_by_id("forum_link").click()

    assert browser.current_url == 'https://forum.threesixtygiving.org/c/grantnav/8'


def test_nav_menu_help_link(provenance_dataload, server_url, browser):
    browser.get(server_url)
    browser.find_element_by_id("help_link").click()

    assert browser.current_url == server_url + '/help'


@pytest.mark.parametrize(('link_text'), [
    ('About'),
    ('Funders'),
    ('Recipients'),
    ('Terms and Conditions'),
    ('Take Down Policy'),
    ('Data Used in GrantNav'),
    ('Reusing GrantNav Data'),
    ('Developers')
    ])
def test_footer_links(provenance_dataload, server_url, browser, link_text):
    browser.get(server_url)
    browser.find_element_by_link_text(link_text)


def test_export_icons_help_link(provenance_dataload, server_url, browser):
    browser.get(server_url)
    browser.find_element_by_class_name("large-search-icon").click()
    browser.find_element_by_id("files_help_link").click()

    assert browser.current_url == server_url + '/help#export_files'


def test_filter_by_help_link(provenance_dataload, server_url, browser):
    browser.get(server_url)
    browser.find_element_by_class_name("large-search-icon").click()
    browser.find_element_by_id("filter_by_help_link").click()

    assert browser.current_url == server_url + '/help#filters'


def test_search(provenance_dataload, server_url, browser):
    browser.get(server_url)
    browser.find_element_by_class_name("large-search-icon").click()
    # Total number of expected grants 4,764
    assert '1,764' in browser.find_element_by_tag_name('body').text
    assert 'Lloyds Bank Foundation for England and Wales (4,116)' in browser.find_element_by_tag_name('body').text
    assert 'Wolfson Foundation (379)' in browser.find_element_by_tag_name('body').text
    other_currencies_modal = browser.find_element_by_id("other-currencies-modal")
    assert other_currencies_modal.text == '7'
    other_currencies_modal.click()
    time.sleep(0.5)
    assert "$146,325" in browser.find_element_by_tag_name('body').text


def test_search_by_titles_and_descriptions_radio_button_in_home(provenance_dataload, server_url, browser):
    browser.get(server_url)

    assert "Titles & Descriptions" in browser.find_element_by_tag_name('body').text


def test_search_by_titles_and_descriptions_radio_button_in_search(provenance_dataload, server_url, browser):
    browser.get(server_url)
    browser.find_element_by_class_name("large-search-icon").click()

    assert "Titles & Descriptions" in browser.find_element_by_tag_name('body').text


def test_search_by_titles_and_descriptions(provenance_dataload, server_url, browser):
    browser.get(server_url)
    # select title_and_description from dropdown menu
    search_dropdown = Select(browser.find_element_by_class_name("front_search"))
    search_dropdown.select_by_value("title_and_description")
    # search "laboratory"
    search_box = browser.find_element_by_class_name("large-search")
    search_box.send_keys('laboratory')
    browser.find_element_by_class_name("large-search-icon").click()

    assert "New science laboratory" in browser.find_element_by_tag_name('body').text
    assert "laboratories" in browser.find_element_by_tag_name('body').text
    assert "£4,846,774" in browser.find_element_by_tag_name('body').text
    # result in "Search All" query
    assert "£4,991,774" not in browser.find_element_by_tag_name('body').text

    browser.get(server_url)
    # search "laboratory" in "Search All"
    search_box = browser.find_element_by_class_name("large-search")
    search_box.send_keys('laboratory')
    browser.find_element_by_class_name("large-search-icon").click()

    assert "New science laboratory" in browser.find_element_by_tag_name('body').text
    assert "laboratories" in browser.find_element_by_tag_name('body').text
    assert "£4,991,774" in browser.find_element_by_tag_name('body').text
    # result in "Titles and Descriptions" query.
    assert "£4,846,774" not in browser.find_element_by_tag_name('body').text


def test_search_current_url(provenance_dataload, server_url, browser):
    browser.get(server_url)
    browser.find_element_by_class_name("large-search-icon").click()

    current_url_split_by_json_query = browser.current_url.split('?')
    assert current_url_split_by_json_query[0][-6:] == 'search'


def test_search_two_words_without_quotes(provenance_dataload, server_url, browser):
    """
    When a user's search query is 2+ words without quotes,
    we want to inform the user that with quotes will have a better search result.
    """
    browser.get(server_url)
    search_box = browser.find_element_by_class_name("large-search")
    search_box.send_keys('social change')
    browser.find_element_by_class_name("large-search-icon").click()

    assert 'If you\'re looking for a specific phrase, put quotes around it to refine your search. e.g. "youth clubs".' \
           in browser.find_element_by_tag_name('body').text


def test_search_two_words_with_single_quotes(provenance_dataload, server_url, browser):
    browser.get(server_url)
    search_box = browser.find_element_by_class_name("large-search")
    search_box.send_keys("'social change'")
    browser.find_element_by_class_name("large-search-icon").click()

    assert 'If you\'re looking for a specific phrase, put quotes around it to refine your search. e.g. "youth clubs".' \
           not in browser.find_element_by_tag_name('body').text


def test_search_two_words_with_double_quotes(provenance_dataload, server_url, browser):
    browser.get(server_url)
    search_box = browser.find_element_by_class_name("large-search")
    search_box.send_keys('"social change"')
    browser.find_element_by_class_name("large-search-icon").click()

    assert 'If you\'re looking for a specific phrase, put quotes around it to refine your search. e.g. "youth clubs".' \
           not in browser.find_element_by_tag_name('body').text


def test_search_includes_and(provenance_dataload, server_url, browser):
    """
    When a user's search query includes 'and', we want to inform the user of what it means.
    """
    browser.get(server_url)
    search_box = browser.find_element_by_class_name("large-search")
    search_box.send_keys('mental and health')
    browser.find_element_by_class_name("large-search-icon").click()

    assert 'The AND keyword (not case-sensitive) means that results must have both words present. ' \
           'If you\'re looking for a phrase that has the word "and" in it, put quotes around the phrase ' \
           '(e.g. "fees and costs").' in browser.find_element_by_tag_name('body').text


def test_search_does_not_include_and(provenance_dataload, server_url, browser):
    browser.get(server_url)
    search_box = browser.find_element_by_class_name("large-search")
    search_box.send_keys('secondhand clothes')
    browser.find_element_by_class_name("large-search-icon").click()

    assert 'The AND keyword (not case-sensitive) means that results must have both words present. ' \
           'If you\'re looking for a phrase that has the word "and" in it, put quotes around the phrase ' \
           '(e.g. "fees and costs").' not in browser.find_element_by_tag_name('body').text


def test_search_includes_or(provenance_dataload, server_url, browser):
    """
    When a user's search query includes 'or', we want to inform the user of what it means.
    """
    browser.get(server_url)
    search_box = browser.find_element_by_class_name("large-search")
    search_box.send_keys('mental or health')
    browser.find_element_by_class_name("large-search-icon").click()

    assert 'The OR keyword (not case-sensitive) means that results must have one of the words present. ' \
           'This is the default. If you\'re looking for a phrase that has the word "or" in ' \
           '(e.g. "children or adults"), put quotes around it.' in browser.find_element_by_tag_name('body').text


def test_search_does_not_include_or(provenance_dataload, server_url, browser):
    browser.get(server_url)
    search_box = browser.find_element_by_class_name("large-search")
    search_box.send_keys('meteor clothes')
    browser.find_element_by_class_name("large-search-icon").click()

    assert 'The OR keyword (not case-sensitive) means that results must have one of the words present. ' \
           'This is the default. If you\'re looking for a phrase that has the word "or" in ' \
           '(e.g. "children or adults"), put quotes around it.' not in browser.find_element_by_tag_name('body').text


def test_search_display_tip(provenance_dataload, server_url, browser):
    """
    When an advance search message is displayed in the search results,
    'Tip: ' will appear in front of the message.
    """
    browser.get(server_url)
    search_box = browser.find_element_by_class_name("large-search")
    search_box.send_keys('social change')
    browser.find_element_by_class_name("large-search-icon").click()

    assert 'Tip: ' in browser.find_element_by_tag_name('body').text


def test_search_do_not_display_tip(provenance_dataload, server_url, browser):
    browser.get(server_url)
    search_box = browser.find_element_by_class_name("large-search")
    search_box.send_keys('grant')
    browser.find_element_by_class_name("large-search-icon").click()

    assert 'Tip: ' not in browser.find_element_by_tag_name('body').text


def test_search_display_advanced_search_link(provenance_dataload, server_url, browser):
    """
    When an advance search message is displayed in the search results,
    a link to the 'advance search' information page is also included.
    """
    browser.get(server_url)
    search_box = browser.find_element_by_class_name("large-search")
    search_box.send_keys('social change')
    browser.find_element_by_class_name("large-search-icon").click()

    assert 'For more tips, see Advanced Search' in browser.find_element_by_tag_name('body').text


def test_search_advanced_search_correct_link(provenance_dataload, server_url, browser):
    browser.get(server_url)
    search_box = browser.find_element_by_class_name("large-search")
    search_box.send_keys('social change')
    browser.find_element_by_class_name("large-search-icon").click()
    browser.find_element_by_class_name("advanced_search").click()

    assert browser.current_url == 'https://grantnav.threesixtygiving.org/help#advanced_search'


def test_search_do_not_display_advance_search_link(provenance_dataload, server_url, browser):
    browser.get(server_url)
    search_box = browser.find_element_by_class_name("large-search")
    search_box.send_keys('grant')
    browser.find_element_by_class_name("large-search-icon").click()

    assert 'For more tips, see Advanced Search' not in browser.find_element_by_tag_name('body').text


def test_bad_search(provenance_dataload, server_url, browser):
    browser.get(server_url)
    browser.find_element_by_name("text_query").send_keys(" £s:::::afdsfas")
    browser.find_element_by_class_name("large-search-icon").click()
    not_valid = browser.find_element_by_id('not_valid').text
    assert 'Search input is not valid' in not_valid
    assert "We can't find what you tried to search for." in not_valid
    assert "Filter By" in browser.find_element_by_tag_name('h3').text


def test_terms(server_url, browser):
    browser.get(server_url + '/terms')
    assert 'Terms and conditions' in browser.find_element_by_tag_name('h1').text


def test_take_down(server_url, browser):
    browser.get(server_url + '/take_down_policy')
    assert 'Take Down Policy' in browser.find_element_by_tag_name('h1').text


def test_help_page(server_url, browser):
    browser.get(server_url + '/help')
    assert 'Help with using GrantNav' in browser.find_element_by_tag_name('h1').text


def test_developers(server_url, browser):
    browser.get(server_url + '/developers')
    assert 'developers' in browser.find_element_by_tag_name('h1').text


def test_title(server_url, browser):
    browser.get(server_url)
    assert '360Giving GrantNav' in browser.title


def test_no_results_page(server_url, browser):
    # thanks: http://stackoverflow.com/questions/18557275/locating-entering-a-value-in-a-text-box-using-selenium-and-python
    browser.get(server_url)
    inputElement = browser.find_element_by_css_selector(".large-search")
    inputElement.send_keys('dfsergegrdtytdrthgrtyh')
    inputElement.submit()
    no_results = browser.find_element_by_id('no-results').text
    assert 'No Results' in no_results
    assert 'Your search - "dfsergegrdtytdrthgrtyh" - did not match any records.' in no_results
    assert "Filter By" in browser.find_element_by_tag_name('h3').text


@pytest.mark.parametrize(('path'), [
    ('/funder/GB-CHC-327114'),  # funder: Lloyds
    #('/region/South West'),  # region
    ('/recipient/GB-CHC-1092728'),  # recipient: Open Doors
    #('/district/City of Bristol')  # district
    ])
def test_right_align_amounts_in_grant_table(provenance_dataload, server_url, browser, path):
    browser.get(server_url + path)
    grants_table = browser.find_element_by_id('grants_datatable')
    grants_table.find_element_by_css_selector('td.amount')


@pytest.mark.parametrize(('path', 'identifier'), [
    ('/funders', 'funders_datatable'),
    ('/recipients', 'recipients_datatable'),
    ('/funder/GB-CHC-327114', 'recipients_datatable'),  # funder: Lloyds
    ])
def test_right_align_amounts_in_other_tables(provenance_dataload, server_url, browser, path, identifier):
    browser.get(server_url + path)
    table = browser.find_element_by_id(identifier)
    table.find_elements_by_css_selector('td.amount')


def test_datasets_page(server_url, browser):
    browser.get(server_url + '/datasets')
    assert 'Data used in GrantNav' in browser.find_element_by_tag_name('h1').text


@pytest.mark.parametrize(('path', 'identifier', 'text'), [
    ('/funder/GB-CHC-327114', 'disclaimer', 'This data is provided for information purposes only.'),
    ('/funder/GB-CHC-327114', 'disclaimer', 'Please refer to the funder website for details of current grant programmes, application guidelines and eligibility criteria.'),
    ('/grant/360G-LBFEW-111657', 'provenance', 'Where is this data from?'),
    ('/grant/360G-LBFEW-111657', 'provenance', 'This data was originally published by')
    ])
def test_disclaimers(server_url, browser, path, identifier, text):
    browser.get(server_url + path)
    assert text in browser.find_element_by_id(identifier).text


def test_currency_facet(provenance_dataload, server_url, browser):
    browser.get(server_url)
    browser.find_element_by_class_name("large-search-icon").click()
    browser.find_element_by_link_text("USD (4)").click()
    assert 'USD 0 - USD 500' in browser.find_element_by_tag_name('body').text


def test_amount_awarded_facet(provenance_dataload, server_url, browser):
    browser.get(server_url)
    browser.find_element_by_class_name("large-search-icon").click()
    browser.find_element_by_link_text("£1,000 - £5,000 (42)").click()
    total_grants = browser.find_elements_by_css_selector(".top-stats-search dd")[0].text
    assert "42" in total_grants, "Expected number of grants not found"


@pytest.mark.parametrize(('path'), ['/grant/360G-wolfson-19916'])
def test_zero_grant_info_link_present(provenance_dataload, server_url, browser, path):
    browser.get(server_url + path)
    browser.find_element_by_id("zero_value_grant_help_link").click()
    assert browser.current_url == server_url + '/help#zero_value_grants'


@pytest.mark.parametrize(('path'), ['/grant/360G-LBFEW-99233'])
def test_zero_grant_info_link_absent(provenance_dataload, server_url, browser, path):
    browser.get(server_url + path)
    assert len(browser.find_elements_by_id('zero_value_grant_help_link')) == 0
