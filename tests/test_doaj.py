from pathlib import Path
import sys,unittest
from unittest.mock import patch
from urllib.parse import parse_qs,unquote,urlsplit
ROOT=Path(__file__).resolve().parents[1]; SRC=ROOT/"src"
sys.path[:]=[str(SRC)]+[x for x in sys.path if x!=str(SRC)]
from sira.providers.doaj import DOAJProvider

class DOAJProviderTests(unittest.TestCase):
    def test_public_search(self):
        payload={"results":[{"id":"abc12345","bibjson":{
            "title":"Testing","abstract":"A","year":"2026",
            "author":[{"name":"R"}],
            "identifier":[{"type":"doi","id":"10.1/x"}],
            "link":[{"type":"fulltext","content_type":"PDF","url":"https://example.org/a.pdf"}]}}]}
        with patch("sira.providers.doaj.request_json",return_value=payload) as m:
            b=DOAJProvider().search("software testing",1)
        u=urlsplit(m.call_args.args[0].full_url)
        self.assertEqual(u.hostname,"doaj.org")
        self.assertTrue(u.path.startswith("/api/search/articles/"))
        self.assertEqual(unquote(u.path.rsplit("/",1)[-1]),"software testing")
        self.assertEqual(parse_qs(u.query)["pageSize"],["1"])
        self.assertEqual(b.papers[0].doi,"10.1/x")
    def test_bad_row_isolated(self):
        with patch("sira.providers.doaj.request_json",return_value={
            "results":[{"id":"bad","bibjson":{}},{"id":"good12345","bibjson":{"title":"Good"}}]}):
            b=DOAJProvider().search("x",2)
        self.assertEqual((len(b.papers),b.rejected_papers),(1,1))
    def test_invalid_limit_no_network(self):
        with patch("sira.providers.doaj.request_json") as m:
            with self.assertRaises(ValueError): DOAJProvider().search("x",0)
        m.assert_not_called()
