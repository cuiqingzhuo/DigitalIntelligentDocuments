import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:shared_preferences/shared_preferences.dart';

typedef Json = Map<String, dynamic>;
Json object(dynamic value) => Map<String, dynamic>.from(value as Map);
List<Json> objects(dynamic value) =>
    ((value as List?) ?? const []).map(object).toList();

String key() {
  final random = Random.secure();
  return List.generate(
    16,
    (_) => random.nextInt(256).toRadixString(16).padLeft(2, '0'),
  ).join();
}

String normalized(dynamic input) {
  final source = input?.toString().toLowerCase() ?? '';
  final folded = String.fromCharCodes(
    source.runes.map(
      (rune) => rune >= 0xff01 && rune <= 0xff5e ? rune - 0xfee0 : rune,
    ),
  );
  return folded.toLowerCase().replaceAll(
    RegExp(r'[^0-9a-zÀ-ž\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]'),
    '',
  );
}

String isbn13(String number) {
  if (!RegExp(r'^[0-9]{9}[0-9x]$').hasMatch(number)) return number;
  var check = 0;
  for (var i = 0; i < 10; i++) {
    check += (10 - i) * (number[i] == 'x' ? 10 : int.parse(number[i]));
  }
  if (check % 11 != 0) return number;
  final prefix = '978${number.substring(0, 9)}';
  var sum = 0;
  for (var i = 0; i < 12; i++) {
    sum += int.parse(prefix[i]) * (i.isEven ? 1 : 3);
  }
  return '$prefix${(10 - sum % 10) % 10}';
}

class ApiProblem implements Exception {
  ApiProblem(this.code, this.detail);
  final int code;
  final String detail;
  @override
  String toString() => detail;
}

class Pilot {
  Pilot(this.preferences, this.server);
  final SharedPreferences preferences;
  final String server;
  final HttpClient client = HttpClient()
    ..connectionTimeout = const Duration(seconds: 4);

  String get _prefix => 'diw:$server:';

  Json? cached(String name) {
    final text = preferences.getString(_prefix + name);
    return text == null ? null : object(jsonDecode(text));
  }

  Future<void> cache(String name, Json data) async {
    await preferences.setString(_prefix + name, jsonEncode(data));
  }

  Future<Json> request(String path, {Json? body, String? requestKey}) async {
    final url = Uri.parse(server).resolve(path);
    final request = await client
        .openUrl(body == null ? 'GET' : 'POST', url)
        .timeout(const Duration(seconds: 5));
    if (requestKey != null) request.headers.set('Idempotency-Key', requestKey);
    if (body != null) {
      request.headers.contentType = ContentType.json;
      request.write(jsonEncode(body));
    }
    final response = await request.close().timeout(
      Duration(seconds: path.endsWith('/recognitions') ? 65 : 9),
    );
    final raw = await response
        .transform(utf8.decoder)
        .join()
        .timeout(const Duration(seconds: 9));
    Json data;
    try {
      data = object(jsonDecode(raw));
    } on FormatException {
      throw ApiProblem(response.statusCode, '服务器未返回有效的接口数据');
    }
    if (response.statusCode >= 400) {
      final detail = data['detail'];
      throw ApiProblem(
        response.statusCode,
        detail is String ? detail : '接口请求失败（${response.statusCode}）',
      );
    }
    return data;
  }

  List<Json> books(Json? data, String keyword, String field) {
    final needle = normalized(keyword);
    final selections = object(data?['copies'] ?? {});
    return objects(data?['books']).where((book) {
      final fields = {
        'title': [book['title']],
        'author': [book['author']],
        'isbn': [book['isbn']],
        'call_number': objects(selections[book['edition_id']])
            .map((copy) => copy['call_number']),
      };
      return needle.isEmpty ||
          fields.entries.any(
            (entry) =>
                (field == 'all' || field == entry.key) &&
                entry.value.any(
                  (item) =>
                      normalized(item).contains(needle) ||
                      (entry.key == 'isbn' &&
                          normalized(item) == isbn13(needle)),
                ),
          );
    }).toList();
  }

  List<Json> pending() => objects(cached('queue')?['items']);
  Future<void> writeQueue(List<Json> list) => cache('queue', {'items': list});
  void close() => client.close();
}

String readMarker(String value, String library, String floor) {
  final raw = value.trim();
  final pattern = RegExp(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$');
  if (pattern.hasMatch(raw)) return raw.toUpperCase();
  final uri = Uri.tryParse(raw);
  if (uri == null) throw const FormatException('无法读取此架标');
  var parts = uri.pathSegments;
  if ((uri.scheme == 'http' || uri.scheme == 'https') &&
      parts.length == 4 &&
      parts.first == 'shelf') {
    parts = parts.sublist(1);
  } else if (uri.scheme != 'diw' || uri.host != 'shelf' || parts.length != 3) {
    throw const FormatException('请扫描正确的架标二维码');
  }
  if (parts[0] != library || parts[1] != floor) {
    throw const FormatException('此架标属于其他馆或楼层');
  }
  if (!pattern.hasMatch(parts[2])) throw const FormatException('架面编号格式错误');
  return parts[2].toUpperCase();
}

List<Json> route(Json map, String from, String face) {
  final nodes = {
    for (final item in objects(map['nodes'])) item['id'] as String: item,
  };
  final faces = {
    for (final item in objects(map['faces'])) item['id'] as String: item,
  };
  final origin = faces[from]?['node_id'] as String? ?? from;
  final target = faces[face]?['node_id'] as String?;
  if (!nodes.containsKey(origin) ||
      target == null ||
      !nodes.containsKey(target) ||
      faces[face]?['marker_status'] == 'retired')
    return [];
  final graph = {for (final id in nodes.keys) id: <String>[]};
  for (final edge in objects(map['edges'])) {
    final a = edge['a'] as String, b = edge['b'] as String;
    if (edge['accessible'] == true &&
        graph.containsKey(a) &&
        graph.containsKey(b)) {
      graph[a]!.add(b);
      graph[b]!.add(a);
    }
  }
  double distance(String a, String b) {
    final ax = nodes[a]!['x'] as num, ay = nodes[a]!['y'] as num;
    final bx = nodes[b]!['x'] as num, by = nodes[b]!['y'] as num;
    return sqrt(pow(ax - bx, 2) + pow(ay - by, 2));
  }

  final open = <String>{origin},
      costs = <String, double>{origin: 0},
      previous = <String, String>{};
  while (open.isNotEmpty) {
    final current = open.reduce(
      (a, b) =>
          costs[a]! + distance(a, target) <= costs[b]! + distance(b, target)
          ? a
          : b,
    );
    if (current == target) {
      final result = [current];
      while (previous.containsKey(result.last)) {
        result.add(previous[result.last]!);
      }
      return result.reversed.map((id) => nodes[id]!).toList();
    }
    open.remove(current);
    for (final next in graph[current]!) {
      final cost = costs[current]! + distance(current, next);
      if (cost >= (costs[next] ?? double.infinity)) continue;
      costs[next] = cost;
      previous[next] = current;
      open.add(next);
    }
  }
  return [];
}

List<List<double>> points(Json observation) => (observation['quad'] as List)
    .map(
      (point) =>
          (point as List).map((number) => (number as num).toDouble()).toList(),
    )
    .toList();

double midpoint(Json observation, int axis) =>
    points(observation).map((point) => point[axis]).reduce((a, b) => a + b) / 4;

double boxHeight(Json observation) {
  final y = points(observation).map((point) => point[1]);
  return y.reduce(max) - y.reduce(min);
}

double similarity(String a, String b) {
  if (a.isEmpty || b.isEmpty) return 0;
  if (a == b) return 1;
  final x = a.runes.toList(), y = b.runes.toList();
  var previous = List<int>.generate(y.length + 1, (i) => i);
  for (var i = 0; i < x.length; i++) {
    final row = List<int>.filled(y.length + 1, 0)..[0] = i + 1;
    for (var j = 0; j < y.length; j++) {
      row[j + 1] = min(
        min(row[j] + 1, previous[j + 1] + 1),
        previous[j] + (x[i] == y[j] ? 0 : 1),
      );
    }
    previous = row;
  }
  final edit = 1 - previous.last / max(x.length, y.length);
  final contains = a.contains(b) || b.contains(a);
  return max(
    edit * .96,
    contains ? .65 + .3 * min(x.length, y.length) / max(x.length, y.length) : 0,
  );
}

Json offlineMatch({
  required List<Json> observations,
  required List<dynamic> quality,
  required Json edition,
  required Json catalog,
  required String? holdingId,
  required String capture,
  required int duration,
}) {
  final rows = <List<Json>>[];
  final ordered = [...observations]
    ..sort((a, b) => midpoint(a, 1).compareTo(midpoint(b, 1)));
  for (final item in ordered) {
    final band = rows.indexWhere(
      (row) =>
          (midpoint(row.first, 1) - midpoint(item, 1)).abs() <
          .43 * min(boxHeight(row.first), boxHeight(item)),
    );
    if (band < 0) {
      rows.add([item]);
    } else {
      rows[band].add(item);
    }
  }
  final candidates = <Json>[], books = objects(catalog['books']);
  final copies = object(catalog['copies'] ?? {});
  final target = edition['edition_id'];
  if (!books.any((book) => book['edition_id'] == target)) books.add(edition);
  for (var row = 0; row < rows.length; row++) {
    rows[row].sort((a, b) => midpoint(a, 0).compareTo(midpoint(b, 0)));
    for (var col = 0; col < rows[row].length; col++) {
      final item = rows[row][col];
      final raw = item['text'] as String;
      final fragments = raw
          .split(RegExp(r'[\n|；]'))
          .map(normalized)
          .where((s) => s.isNotEmpty)
          .toList();
      if (fragments.isEmpty) continue;
      final scored = <Json>[];
      for (final book in books) {
        final title = normalized(book['title']);
        final fragmentScore = fragments
            .map((part) => similarity(part, title))
            .reduce(max);
        if (fragmentScore < .52 &&
            book['edition_id'] != target &&
            book['isbn'] == null &&
            objects(copies[book['edition_id']]).isEmpty)
          continue;
        var score = .91 * fragmentScore;
        final evidence = <String>[];
        if (fragmentScore >= .55) evidence.add('书名片段');
        final isbn = normalized(book['isbn']);
        if (isbn.isNotEmpty && fragments.any((part) => part.contains(isbn))) {
          evidence.add('ISBN');
          score = max(score, .995);
        }
        final numbers = objects(copies[book['edition_id']])
            .map((copy) => normalized(copy['call_number']));
        if (numbers.any(
          (number) =>
              number.length >= 3 &&
              fragments.any((part) => part == number || part == title + number),
        )) {
          evidence.add('索书号');
          score = max(score, .94);
        }
        final volume = normalized(book['volume']);
        if (volume.isNotEmpty &&
            fragmentScore >= .55 &&
            fragments.any((part) => part.contains(volume))) {
          evidence.add('版次／卷册');
          score = min(1.0, score + .18);
        }
        scored.add({...book, 'score': score, 'evidence': evidence});
      }
      final targets = scored.where((item) => item['edition_id'] == target);
      if (targets.isEmpty) continue;
      final goal = targets.first, score = goal['score'] as double;
      if (score < .53) continue;
      final other =
          scored
              .where(
                (item) =>
                    item['edition_id'] != target &&
                    (item['score'] as double) >= .53,
              )
              .toList()
            ..sort(
              (a, b) => (b['score'] as double).compareTo(a['score'] as double),
            );
      final evidence = (goal['evidence'] as List).cast<String>();
      const explicit = {'ISBN', '索书号', '版次／卷册'};
      if (other.any(
        (value) => (value['evidence'] as List).any(
          (tag) => explicit.contains(tag) && !evidence.contains(tag),
        ),
      ))
        continue;
      final targetExplicit = evidence.any(explicit.contains);
      final rival = other
          .map(
            (item) =>
                (item['score'] as double) -
                (targetExplicit &&
                        !(item['evidence'] as List).any(explicit.contains)
                    ? .14
                    : 0.0),
          )
          .fold(0.0, max);
      if (rival - score >= .14) continue;
      candidates.add({
        ...item,
        'observation_index': observations.indexOf(item),
        'row': row + 1,
        'column': col + 1,
        'column_scope': capture == 'left_visible' ? 'shelf_row' : 'image',
        'score': score,
        'evidence': evidence,
        'separation': score - rival,
        'alternatives': other.take(2).toList(),
      });
    }
  }
  candidates.sort(
    (a, b) => (b['score'] as double).compareTo(a['score'] as double),
  );
  var state = quality.isNotEmpty ? 'retake' : 'unrecognized';
  if (quality.isEmpty && candidates.isNotEmpty) {
    final top = candidates.first['score'] as double,
        rival = candidates.length > 1 ? candidates[1]['score'] as double : 0.0;
    state =
        holdingId == null &&
            top >= .86 &&
            candidates.first['separation'] >= .14 &&
            top - rival >= .14
        ? 'unique'
        : 'confirm';
  }
  return {
    'state': state,
    'candidates': candidates
        .take(
          state == 'unique'
              ? 1
              : state == 'confirm'
              ? 3
              : 0,
        )
        .toList(),
    'quality': quality,
    'model_version': 'android-local-v1',
    'source': 'android_local',
    'duration_ms': duration,
    'capture_scope': capture,
  };
}
