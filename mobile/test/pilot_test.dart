import 'package:diw_reader/pilot.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  const target = {'edition_id': 'third', 'title': '数据结构', 'volume': '第3版'};
  const previous = {'edition_id': 'second', 'title': '数据结构', 'volume': '第2版'};
  const catalog = {
    'books': [target, previous],
    'copies': {
      'third': [
        {'call_number': 'TP311/03'},
      ],
      'second': [
        {'call_number': 'TP311/02'},
      ],
    },
  };
  Json observation(double left, String text) => {
    'quad': [
      [left, .1],
      [left + .1, .1],
      [left + .1, .8],
      [left, .8],
    ],
    'text': text,
    'direction': 'horizontal',
  };
  Json match(String reading, {String? holding, String capture = 'partial'}) =>
      offlineMatch(
        observations: [observation(.7, reading), observation(.3, '算法导论')],
        quality: [],
        edition: target,
        catalog: catalog,
        holdingId: holding,
        capture: capture,
        duration: 10,
      );

  test(
    'offline reading preserves image order and original observation index',
    () {
      final answer = match('数据结构\n第3版\nTP311/03');
      final candidate = objects(answer['candidates']).single;
      expect(answer['state'], 'unique');
      expect(candidate['column'], 2);
      expect(candidate['column_scope'], 'image');
      expect(candidate['observation_index'], 0);
    },
  );

  test('absent and same-title wrong edition do not produce a unique hint', () {
    expect(match('数据结构\n第2版\nTP311/02')['state'], 'unrecognized');
    expect(match('数据库系统')['state'], 'unrecognized');
    expect(match('数据结构')['state'], 'confirm');
    final copy = match(
      '数据结构\n第3版',
      holding: 'holding-1',
      capture: 'left_visible',
    );
    expect(copy['state'], 'confirm');
    expect(objects(copy['candidates']).single['column_scope'], 'shelf_row');
  });

  test('physical marker identifies the correct library and floor', () {
    expect(readMarker('diw://shelf/campus/F1/A01-B', 'campus', 'F1'), 'A01-B');
    expect(
      () => readMarker('diw://shelf/neighbor/F1/A01-A', 'campus', 'F1'),
      throwsFormatException,
    );
  });

  test('offline routing respects blocked segments', () {
    final map = {
      'nodes': [
        {'id': 'entrance', 'x': 1, 'y': 1},
        {'id': 'aisle', 'x': 3, 'y': 1},
        {'id': 'face', 'x': 3, 'y': 3},
      ],
      'edges': [
        {'a': 'entrance', 'b': 'face', 'accessible': false},
        {'a': 'entrance', 'b': 'aisle', 'accessible': true},
        {'a': 'aisle', 'b': 'face', 'accessible': true},
      ],
      'faces': [
        {'id': 'A01-A', 'node_id': 'face', 'marker_status': 'active'},
      ],
    };
    expect(route(map, 'entrance', 'A01-A').map((item) => item['id']), [
      'entrance',
      'aisle',
      'face',
    ]);
  });
}
