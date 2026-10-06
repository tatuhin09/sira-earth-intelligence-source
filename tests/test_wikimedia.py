from pathlib import Path
import sys,unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit
ROOT=Path(__file__).resolve().parents[1]; SRC=ROOT/"src"
sys.path[:]=[str(SRC)]+[x for x in sys.path if x!=str(SRC)]
from sira.providers.wikimedia import WikimediaProvider

class WikimediaProviderTests(unittest.TestCase):
    def test_public_search(self):
        payload={"pages":[{"id":1,"key":"Python_(programming_language)",
                           "title":"Python","description":"Language"}]}
        with patch("sira.providers.wikimedia.request_json",return_value=payload) as m:
            b=WikimediaProvider().search("python",1)
        u=urlsplit(m.call_args.args[0].full_url)
        self.assertEqual(u.hostname,"en.wikipedia.org")
        self.assertEqual(u.path,"/w/rest.php/v1/search/page")
        self.assertEqual(parse_qs(u.query)["limit"],["1"])
        self.assertEqual(b.records[0].provider,"wikimedia")
    def test_bad_row_isolated(self):
        with patch("sira.providers.wikimedia.request_json",return_value={
            "pages":[{"id":"x","key":"Bad","title":"Bad"},{"id":2,"key":"Good","title":"Good"}]}):
            b=WikimediaProvider().search("good",2)
        self.assertEqual((len(b.records),b.rejected_records),(1,1))
    def test_invalid_limit_no_network(self):
        with patch("sira.providers.wikimedia.request_json") as m:
            with self.assertRaises(ValueError): WikimediaProvider().search("x",4)
        m.assert_not_called()
