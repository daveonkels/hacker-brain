import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import context
import search


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.temp.name)
        self.items = [
            {'id': 3, 'type': 'comment', 'parent': 2, 'by': 'alice', 'time': 100,
             'text': 'Been using GLM via Ollama for ESP 32. It worked.<p>Full ending retained.'},
            {'id': 4, 'type': 'comment', 'parent': 3, 'by': 'bob', 'time': 101, 'text': 'What is your main model?'},
            {'id': 5, 'type': 'comment', 'parent': 4, 'by': 'alice', 'time': 102, 'text': 'Actually GLM is my fallback.'},
            {'id': 6, 'type': 'comment', 'parent': 2, 'by': 'spam', 'time': 103, 'text': 'Ollama ESP32', 'dead': True},
            {'id': 7, 'type': 'comment', 'parent': 2, 'time': 104, 'deleted': True},
        ]
        self.older = [
            {'id': 1, 'type': 'story', 'title': 'An unrelated headline', 'time': 80},
            {'id': 2, 'type': 'comment', 'parent': 1, 'by': 'bob', 'time': 90, 'text': 'I used Ollama for ESP32 before.'},
        ]
        self.write('items.ndjson', self.items)
        self.write('context.ndjson', self.older)
        (self.folder / 'summary.json').write_text('{}')
        (self.folder / 'context-summary.json').write_text('{}')
        search.build(self.folder)
        self.db = search.connect(self.folder / 'search.sqlite3')

    def write(self, name, items):
        (self.folder / name).write_text(''.join(json.dumps(i)+'\n' for i in items))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_nested_comment_found_despite_unrelated_title(self):
        r = search.search_items(self.db, {'q': 'Ollama ESP32'})
        self.assertEqual([i['id'] for i in r['items']], [3])
        self.assertIn('Full ending retained.', r['items'][0]['body'])

    def test_older_context_opt_in_and_dead_exclusion(self):
        r = search.search_items(self.db, {'q': 'Ollama ESP32', 'older': '1'})
        self.assertEqual({i['id'] for i in r['items']}, {2, 3})

    def test_followup_keeps_intervening_question(self):
        c = search.conversation(self.db, 3)
        self.assertEqual([i['id'] for i in c['ancestors']], [1, 2])
        self.assertEqual(c['followups'][0]['item']['id'], 5)
        self.assertEqual([i['id'] for i in c['followups'][0]['path']], [4])

    def test_phrase_and_literal_operators(self):
        self.assertEqual(search.search_items(self.db, {'q': '"Full ending"'})['total'], 1)
        self.assertEqual(search.search_items(self.db, {'q': 'Ollama OR nosuchword'})['total'], 0)
        self.assertEqual(search.search_items(self.db, {'q': 'Ollama nosuchword', 'match': 'any'})['total'], 1)

    def test_firsthand_filter_is_wording_not_verification(self):
        r = search.search_items(self.db, {'q': 'GLM', 'experience': '1'})
        self.assertEqual([i['id'] for i in r['items']], [3])
        self.assertNotIn('Firsthand wording', search.experience_signals('We have awkward tech bros. I have to say it.'))
        self.assertIn('Firsthand wording', search.experience_signals('I’ve been using this for a month.'))

    def test_html_is_plain_text(self):
        self.assertEqual(search.plain('<p>A &amp; B</p><script>alert(1)</script><p>C</p>'), 'A & B\n\nC')

    def test_backfill_recurses_and_preserves_sample(self):
        (self.folder / 'context.ndjson').unlink()
        before = (self.folder / 'items.ndjson').read_bytes()
        source = {i['id']: i for i in self.older}
        with patch.object(context, 'fetch', side_effect=lambda route: source[int(route.split('/')[1])]) as fetch:
            result = context.backfill(self.folder, workers=2)
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result['unresolved_parent_ids'], [])
        self.assertEqual((self.folder / 'items.ndjson').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
