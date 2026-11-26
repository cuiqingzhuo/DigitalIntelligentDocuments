import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/services.dart';
import 'package:image_picker/image_picker.dart';
import 'package:material_ui/material_ui.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'pilot.dart';

const green = Color(0xff12674f);
const defaultServer = String.fromEnvironment(
  'DIW_API',
  defaultValue: 'http://10.0.2.2:8000',
);
const vision = MethodChannel('cn.diw.reader/vision');

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(ReaderApp(await SharedPreferences.getInstance()));
}

class ReaderApp extends StatelessWidget {
  const ReaderApp(this.preferences, {super.key});
  final SharedPreferences preferences;

  @override
  Widget build(BuildContext context) => MaterialApp(
    title: '数智文枢',
    locale: const Locale('zh', 'CN'),
    supportedLocales: const [Locale('zh', 'CN')],
    localizationsDelegates: GlobalMaterialLocalizations.delegates,
    debugShowCheckedModeBanner: false,
    theme: ThemeData(
      useMaterial3: true,
      colorScheme: ColorScheme.fromSeed(
        seedColor: green,
        primary: green,
        surface: Colors.white,
      ),
      scaffoldBackgroundColor: const Color(0xfff4f7f4),
      inputDecorationTheme: InputDecorationTheme(
        border: OutlineInputBorder(borderRadius: BorderRadius.circular(12)),
        filled: true,
        fillColor: Colors.white,
      ),
    ),
    home: ReaderScreen(preferences),
  );
}

class ReaderScreen extends StatefulWidget {
  const ReaderScreen(this.preferences, {super.key});
  final SharedPreferences preferences;

  @override
  State<ReaderScreen> createState() => _ReaderScreenState();
}

class _ReaderScreenState extends State<ReaderScreen>
    with WidgetsBindingObserver {
  late Pilot pilot;
  final picker = ImagePicker();
  final query = TextEditingController();
  final marker = TextEditingController();
  final barcode = TextEditingController();
  final note = TextEditingController();
  final manualFace = TextEditingController();
  final scroll = ScrollController();
  Json settings = {}, catalog = {}, floorMap = {};
  List<Json> books = [], availableCopies = [];
  Json? selected, activeSession, result, verification, receipt;
  String field = 'all',
      start = '',
      chosenFace = '',
      actualFace = '',
      scannedMarker = '';
  String? holdingId, photo, photoPreview, workorder;
  String capture = 'partial', reason = 'unrecognized', sessionKey = key();
  String notice = '';
  int step = 0, page = 1, total = 0;
  bool online = false,
      busy = false,
      flushing = false,
      consent = false,
      large = false;
  List<Json> queue = [];
  Timer? timer;
  Future<void>? syncing;

  String get library => settings['library'] as String? ?? 'demo';
  String get floor => settings['floor'] as String? ?? 'F1';
  Json get document => object(floorMap['document'] ?? {});
  List<Json> get faces =>
      objects(document['faces'])
          .where((face) => face['marker_status'] != 'retired')
          .toList();
  String get help => document['service_label'] as String? ?? '楼层入口服务台';
  bool get demo => settings['demo'] == true;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    pilot = Pilot(
      widget.preferences,
      widget.preferences.getString('diw:server') ?? defaultServer,
    );
    loadLocal();
    vision.setMethodCallHandler((call) async {
      if (call.method == 'markerAvailable' && mounted && !busy) {
        await run(resolveShelfIntent);
      }
    });
    unawaited(boot());
    timer = Timer.periodic(const Duration(seconds: 25), (_) {
      if (!busy && (!online || queue.isNotEmpty)) unawaited(sync());
    });
  }

  void loadLocal() {
    settings = pilot.cached('config') ?? {};
    catalog = pilot.cached('catalog') ?? {};
    floorMap = pilot.cached('map') ?? {};
    queue = pilot.pending();
    start = floorMap['start'] as String? ?? '';
    books = pilot.books(catalog, '', 'all').take(15).toList();
    total = objects(catalog['books']).length;
  }

  Future<void> boot() async {
    await run(() async {
      final saved = pilot.cached('active');
      if (saved != null && saved['edition'] is Map) {
        selected = object(saved['edition']);
        holdingId = saved['holding_id'] as String?;
        sessionKey = saved['session_key'] as String;
        chosenFace = saved['face'] as String? ?? '';
        actualFace = saved['actual'] as String? ?? '';
        scannedMarker = saved['scanned'] as String? ?? '';
        activeSession = saved['session'] == null
            ? null
            : object(saved['session']);
        workorder = saved['workorder'] as String?;
        verification = saved['verification'] == null
            ? null
            : object(saved['verification']);
        receipt = saved['receipt'] == null ? null : object(saved['receipt']);
        step = activeSession == null
            ? 1
            : verification == null
            ? 2
            : 3;
      }
      await refresh();
      final recovery = await picker.retrieveLostData();
      if (recovery.files?.isNotEmpty == true &&
          activeSession != null &&
          saved?['picking'] == true) {
        await setPhoto(recovery.files!.first);
      }
      await resolveShelfIntent();
      await saveTask();
    });
  }

  Future<void> refresh() async {
    try {
      settings = await pilot.request('/config');
      await pilot.cache('config', settings);
      online = true;
    } on Exception {
      online = false;
      if (catalog.isEmpty) notice = '首次离线，暂时没有本馆书目和地图。可在设置中检查服务地址。';
      return;
    }
    final responses = await Future.wait([
      pilot.request('/maps/${Uri.encodeComponent(floor)}'),
      pilot.request('/cache/pilot'),
    ]);
    floorMap = responses[0];
    catalog = responses[1];
    start = floorMap['start'] as String? ?? '';
    await Future.wait([
      pilot.cache('map', floorMap),
      pilot.cache('catalog', catalog),
    ]);
    await search();
    await flush();
  }

  Future<void> saveTask({bool picking = false}) => pilot.cache('active', {
    'edition': selected,
    'holding_id': holdingId,
    'session_key': sessionKey,
    'face': chosenFace,
    'actual': actualFace,
    'scanned': scannedMarker,
    'session': activeSession,
    'workorder': workorder,
    'verification': verification,
    'receipt': receipt,
    'picking': picking,
  });

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed && !busy) {
      unawaited(resolveShelfOnResume());
    }
  }

  Future<void> resolveShelfOnResume() async {
    await run(resolveShelfIntent);
    await sync();
  }

  Future<void> resolveShelfIntent() async {
    String? raw;
    try {
      raw = await vision.invokeMethod<String>('readMarkerIntent');
    } on MissingPluginException {
      return;
    }
    if (raw != null && mounted) await arrive(raw, fromScan: true);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    vision.setMethodCallHandler(null);
    scroll.dispose();
    timer?.cancel();
    pilot.close();
    for (final item in [query, marker, barcode, note, manualFace]) {
      item.dispose();
    }
    unawaited(deletePreview());
    super.dispose();
  }

  void goToStep(int next) {
    if (step == next) return;
    step = next;
    if (scroll.hasClients) scroll.jumpTo(0);
  }

  Future<void> run(Future<void> Function() action) async {
    if (busy || !mounted) return;
    setState(() {
      busy = true;
      notice = '';
    });
    try {
      await syncing;
      if (!mounted) return;
      await action();
    } on ApiProblem catch (error) {
      notice = error.detail;
    } on FormatException catch (error) {
      notice = error.message;
    } on PlatformException catch (error) {
      notice = '本机相机或识别暂不可用。可以手动输入架面和条码，或重拍。 ${error.message ?? ''}';
    } on Exception {
      notice = '操作未完成，请检查网络或照片后重试。';
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  Future<void> sync() async {
    if (syncing != null || flushing || busy || !mounted) return;
    final operation = syncPending();
    syncing = operation;
    try {
      await operation;
    } finally {
      syncing = null;
    }
  }

  Future<void> syncPending() async {
    try {
      await pilot.request('/health');
      online = true;
      await flush();
    } on Exception {
      online = false;
    }
    if (mounted) setState(() {});
  }

  Future<void> flush() async {
    if (flushing) return;
    flushing = true;
    try {
      while (queue.isNotEmpty) {
        final item = queue.first;
        final body = object(item['body']);
        final response = await pilot.request(
          item['path'] as String,
          body: body,
          requestKey: item['key'] as String,
        );
        final local = item['local_id'];
        if (local != null) {
          for (final pending in queue.skip(1)) {
            if (pending['session_id'] != local) continue;
            pending['path'] = (pending['path'] as String).replaceAll(
              local as String,
              response['session_id'] as String,
            );
            pending['session_id'] = response['session_id'];
            if ((pending['body'] as Map)['session_id'] == local) {
              pending['body']['session_id'] = response['session_id'];
            }
          }
          if (activeSession?['session_id'] == local) {
            activeSession = response;
            await saveTask();
          }
        } else if (activeSession?['session_id'] == item['session_id']) {
          if (item['path'] == '/feedback') {
            applyFeedback(response);
            await saveTask();
          } else if ((item['path'] as String).endsWith('/verifications')) {
            verification = response;
            if (verification?['current']['outcome'] == 'wrong') {
              reason = 'wrong_edition';
            }
            await saveTask();
          }
        }
        queue.removeAt(0);
        await pilot.writeQueue(queue);
      }
    } on ApiProblem catch (error) {
      notice = '待同步记录需处理：${error.detail}';
    } on Exception {
      online = false;
    } finally {
      flushing = false;
    }
  }

  Future<void> enqueue(Json entry) async {
    if (queue.any((item) => item['key'] == entry['key'])) return;
    queue.add(entry);
    await pilot.writeQueue(queue);
  }

  Future<void> search([int requested = 1]) async {
    const limit = 15;
    page = requested;
    try {
      final params = Uri(
        queryParameters: {
          'q': query.text.trim(),
          'field': field,
          'page': '$page',
          'limit': '$limit',
        },
      );
      final response = await pilot.request('/editions?${params.query}');
      online = true;
      books = objects(response['items']);
      total = response['total'] as int;
    } on ApiProblem {
      rethrow;
    } on Exception {
      online = false;
      final local = pilot.books(catalog, query.text.trim(), field);
      total = local.length;
      books = local.skip((page - 1) * limit).take(limit).toList();
    }
  }

  Future<void> selectBook(Json book) async {
    final id = book['edition_id'] as String;
    List<Json> copies;
    try {
      final data = await pilot.request(
        '/editions/${Uri.encodeComponent(id)}/holdings',
      );
      copies = objects(data['items']);
    } on ApiProblem {
      rethrow;
    } on Exception {
      copies = objects(object(catalog['copies'] ?? {})[id]);
    }
    if (!mounted) return;
    availableCopies = copies
        .where((copy) => copy['status'] == 'available')
        .toList();
    String? picked;
    final choice = await showModalBottomSheet<String>(
      isScrollControlled: true,
      context: context,
      builder: (modal) => StatefulBuilder(
        builder: (context, update) => SafeArea(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(22),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  book['title'] as String,
                  style: Theme.of(context).textTheme.headlineSmall,
                ),
                Text(bookMeta(book)),
                const SizedBox(height: 14),
                if (availableCopies.isEmpty)
                  Text(book['status'] == 'loaned' ? '该版本全部借出。' : '该版本没有可用复本。')
                else ...[
                  DropdownButtonFormField<String>(
                    initialValue: '',
                    decoration: const InputDecoration(labelText: '接受的复本'),
                    isExpanded: true,
                    items: [
                      const DropdownMenuItem(
                        value: '',
                        child: Text('同版任一可用复本'),
                      ),
                      ...availableCopies.map(
                        (copy) => DropdownMenuItem(
                          value: copy['holding_id'] as String,
                          child: Text(
                            '${copy['call_number']} · ${copy['holding_id']}',
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                      ),
                    ],
                    onChanged: (value) => update(() => picked = value),
                  ),
                  const SizedBox(height: 12),
                  FilledButton(
                    onPressed: () => Navigator.pop(modal, picked ?? ''),
                    child: const Text('确认版本'),
                  ),
                ],
              ],
            ),
          ),
        ),
      ),
    );
    if (choice == null) return;
    await clearPhoto();
    selected = book;
    holdingId = choice.isEmpty ? null : choice;
    sessionKey = key();
    activeSession = null;
    result = null;
    verification = null;
    receipt = null;
    workorder = null;
    goToStep(1);
    await saveTask();
    if (actualFace.isNotEmpty && chosenFace == actualFace)
      await arrive(scannedMarker, fromScan: true);
  }

  Future<void> selectFace(String face) async {
    if (chosenFace != face && activeSession != null) {
      activeSession = null;
      sessionKey = key();
      verification = null;
      workorder = null;
      receipt = null;
      await clearPhoto();
    }
    chosenFace = face;
    await saveTask();
  }

  Future<void> arrive(String raw, {bool fromScan = false}) async {
    final id = readMarker(raw, library, floor);
    if (chosenFace.isEmpty && fromScan) chosenFace = id;
    if (chosenFace.isEmpty) throw const FormatException('请先选择目标架面，或扫描架标直接开始');
    Json? location;
    try {
      final params = Uri(
        queryParameters: {'floor': floor, 'expected': chosenFace},
      );
      location = await pilot.request(
        '/shelf-faces/${Uri.encodeComponent(id)}?${params.query}',
      );
      online = true;
    } on ApiProblem {
      rethrow;
    } on Exception {
      online = false;
      for (final face in faces) {
        if (face['id'] == id) location = face;
      }
      if (location == null &&
          floorMap.isNotEmpty &&
          floorMap['revision'] == 0) {
        location = {'id': id, 'direction': null};
      }
      if (location == null) throw FormatException('离线地图中没有此架面，可前往$help');
    }
    actualFace = id;
    scannedMarker = raw;
    marker.text = id;
    if (id != chosenFace) {
      notice = '实际位置 ${location['id']}，目标架面 $chosenFace。可改选或从实际位置继续导航。';
      await saveTask();
      return;
    }
    if (selected == null) {
      goToStep(0);
      notice = '已确认 $library 馆 · $floor · $id，请选定目标图书。';
      await saveTask();
      return;
    }
    final body = <String, dynamic>{
      'edition_id': selected!['edition_id'],
      'holding_id': holdingId,
      'floor': floor,
      'face_id': chosenFace,
      'scanned_face_id': raw,
    };
    try {
      activeSession = await pilot.request(
        '/sessions',
        body: body,
        requestKey: sessionKey,
      );
    } on ApiProblem {
      rethrow;
    } on Exception {
      online = false;
      final local = 'local-$sessionKey';
      await enqueue({
        'path': '/sessions',
        'body': body,
        'key': sessionKey,
        'local_id': local,
      });
      activeSession = {'session_id': local, 'face': location};
    }
    goToStep(2);
    await saveTask();
  }

  Future<String?> scan(ImageSource source) async {
    final file = await picker.pickImage(
      source: source,
      maxWidth: 1800,
      requestFullMetadata: false,
    );
    if (file == null) return null;
    final values =
        await vision.invokeListMethod<String>('barcode', {'path': file.path}) ??
        [];
    if (values.length != 1) {
      throw FormatException(values.isEmpty ? '未读到条码，请重拍或手动输入' : '请一次只拍摄一个条码');
    }
    return values.single;
  }

  Future<void> scanShelf() async {
    final value = await scan(ImageSource.camera);
    if (value != null) await arrive(value, fromScan: true);
  }

  Future<void> deletePreview() async {
    final path = photoPreview;
    photoPreview = null;
    if (path != null) {
      try {
        await File(path).delete();
      } on FileSystemException {
        /* Cache may have been cleared. */
      }
    }
  }

  Future<void> clearPhoto() async {
    photo = null;
    result = null;
    consent = false;
    await deletePreview();
  }

  Future<void> setPhoto(XFile file) async {
    await clearPhoto();
    photo = file.path;
    goToStep(2);
  }

  Future<void> pick(ImageSource source) async {
    await saveTask(picking: true);
    try {
      final file = await picker.pickImage(
        source: source,
        maxWidth: 1800,
        maxHeight: 1800,
        imageQuality: 90,
        requestFullMetadata: false,
      );
      if (file != null) await setPhoto(file);
    } finally {
      await saveTask();
    }
  }

  Future<void> recognizePhoto({bool review = false}) async {
    if (photo == null || activeSession == null || selected == null) return;
    final id = activeSession!['session_id'] as String;
    final path = '/sessions/$id/recognitions';
    if (review) {
      if (!consent || !online || id.startsWith('local-'))
        throw const FormatException('联网并单独同意后才可上传本次照片');
      await flush();
      if (!online || queue.any((item) => item['path'] == path)) {
        throw const FormatException('请先同步本次本机识别记录');
      }
      final file = File(photoPreview ?? photo!);
      final encoded = base64Encode(await file.readAsBytes());
      result = await pilot.request(
        path,
        body: {
          'capture_scope': capture,
          'upload_consent': true,
          'image_base64': encoded,
        },
        requestKey: key(),
      );
      consent = false;
      return;
    }
    final watch = Stopwatch()..start();
    final native = object(
      await vision.invokeMethod('recognize', {'path': photo}),
    );
    await deletePreview();
    photoPreview = native['path'] as String?;
    final observation = objects(native['observations']),
        quality = native['quality'] as List;
    result = offlineMatch(
      observations: observation,
      quality: quality,
      edition: selected!,
      catalog: catalog,
      holdingId: holdingId,
      capture: capture,
      duration: watch.elapsedMilliseconds,
    );
    final body = <String, dynamic>{
      'capture_scope': capture,
      'observations': observation,
      'quality': quality,
      'device_model': native['device_model'],
      'device_ms': watch.elapsedMilliseconds,
      'device_display': {
        'state': result!['state'],
        'model_version': result!['model_version'],
        'candidates': objects(result!['candidates'])
            .map(
              (item) => {
                'observation_index': item['observation_index'],
                'score': item['score'],
                'evidence': item['evidence'],
                'alternatives': objects(item['alternatives'])
                    .map((book) => book['edition_id'])
                    .toList(),
              },
            )
            .toList(),
      },
    };
    final requestKey = key();
    reason = result!['state'] == 'retake' ? 'retake' : 'unrecognized';
    await enqueue({
      'path': path,
      'body': body,
      'key': requestKey,
      'session_id': id,
    });
    if (online) await flush();
  }

  Future<void> verify(String? value) async {
    final id = activeSession?['session_id'] as String?;
    if (id == null) return;
    final path = '/sessions/$id/verifications',
        requestKey = key(),
        body = {'barcode': value};
    if (value != null &&
        !RegExp(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$').hasMatch(value)) {
      throw const FormatException('请输入有效馆藏条码');
    }
    await enqueue({
      'path': path,
      'body': body,
      'key': requestKey,
      'session_id': id,
    });
    if (online) await flush();
    if (notice.isEmpty && queue.any((item) => item['key'] == requestKey)) {
      notice = '核验已排队，联网同步前为待核验。';
    }
  }

  Future<void> feedback() async {
    final id = activeSession?['session_id'] as String?;
    if (id == null) return;
    final body = {'session_id': id, 'reason': reason, 'note': note.text.trim()};
    final pending = queue.where(
      (item) =>
          item['path'] == '/feedback' &&
          jsonEncode(item['body']) == jsonEncode(body),
    );
    final requestKey = pending.isEmpty ? key() : pending.first['key'] as String;
    await enqueue({
      'path': '/feedback',
      'body': body,
      'key': requestKey,
      'session_id': id,
    });
    if (online) await flush();
    if (notice.isEmpty && queue.any((item) => item['key'] == requestKey)) {
      notice = '反馈已离线排队，恢复连接后自动重传。';
    }
  }

  void applyFeedback(Json response) {
    final order = response['workorder_id'] as String?;
    if (order != null) {
      workorder = order;
      receipt = response;
    }
    notice = order == null ? '已保存自报反馈。' : '工单已保存，编号 $order。';
  }

  Future<void> configure() async {
    final address = TextEditingController(text: pilot.server);
    final value = await showDialog<String>(
      context: context,
      builder: (dialog) => AlertDialog(
        title: const Text('服务地址'),
        content: TextField(
          controller: address,
          keyboardType: TextInputType.url,
          decoration: const InputDecoration(labelText: 'http(s)://主机:端口'),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialog),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(dialog, address.text.trim()),
            child: const Text('保存'),
          ),
        ],
      ),
    );
    address.dispose();
    if (value == null || value == pilot.server) return;
    final uri = Uri.tryParse(value);
    if (uri == null ||
        !['http', 'https'].contains(uri.scheme) ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty ||
        uri.hasQuery ||
        uri.hasFragment ||
        (uri.path.isNotEmpty && uri.path != '/')) {
      throw const FormatException('请输入含 http 或 https 的完整服务地址');
    }
    await widget.preferences.setString('diw:server', value);
    pilot.close();
    pilot = Pilot(widget.preferences, value);
    activeSession = null;
    selected = null;
    chosenFace = actualFace = scannedMarker = '';
    sessionKey = key();
    await clearPhoto();
    goToStep(0);
    loadLocal();
    await refresh();
  }

  String date(dynamic value) => value is String
      ? DateTime.tryParse(value)?.toLocal().toString().split('.').first ??
            '未知时间'
      : '暂无';

  String bookMeta(Json book) => [
    book['author'],
    book['publisher'],
    book['year'],
    book['volume'],
    if (book['isbn'] != null) 'ISBN ${book['isbn']}',
  ].where((item) => item != null && item.toString().isNotEmpty).join(' · ');

  Widget section(String title, List<Widget> children) => Card(
    elevation: 0,
    shape: RoundedRectangleBorder(
      borderRadius: BorderRadius.circular(18),
      side: const BorderSide(color: Color(0xffdce5df)),
    ),
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(title, style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(height: 10),
          ...children.map(
            (widget) => Padding(
              padding: const EdgeInsets.only(bottom: 9),
              child: widget,
            ),
          ),
        ],
      ),
    ),
  );

  Widget button(
    String label,
    Future<void> Function() action, {
    bool primary = false,
    bool enabled = true,
  }) {
    final onPress = !busy && enabled ? () => run(action) : null;
    return primary
        ? FilledButton(onPressed: onPress, child: Text(label))
        : OutlinedButton(onPressed: onPress, child: Text(label));
  }

  Widget catalogView() => section('检索书目', [
    TextField(
      controller: query,
      textInputAction: TextInputAction.search,
      onSubmitted: (_) => run(search),
      decoration: const InputDecoration(
        labelText: '书名、作者、ISBN 或索书号',
        prefixIcon: Icon(Icons.search),
      ),
    ),
    DropdownButtonFormField<String>(
      initialValue: field,
      decoration: const InputDecoration(labelText: '检索字段'),
      items: const [
        DropdownMenuItem(value: 'all', child: Text('全部字段')),
        DropdownMenuItem(value: 'title', child: Text('书名')),
        DropdownMenuItem(value: 'author', child: Text('作者')),
        DropdownMenuItem(value: 'isbn', child: Text('ISBN')),
        DropdownMenuItem(value: 'call_number', child: Text('索书号')),
      ],
      onChanged: busy ? null : (value) => setState(() => field = value!),
    ),
    Wrap(
      spacing: 8,
      runSpacing: 8,
      children: [
        button('检索', search, primary: true),
        button('从架标扫码开始', scanShelf),
        if (demo)
          button('查找《数据结构》', () async {
            query.text = '数据结构';
            field = 'all';
            await search();
          }),
      ],
    ),
    if (books.isEmpty) Text(online ? '没有匹配版本。' : '缓存书目中没有匹配的版本。'),
    ...books.map((book) {
      final available = book['available'] as int? ?? 0;
      final stale =
          book['stale'] == true ||
          (DateTime.tryParse(book['synced_at'] as String? ?? '')?.isBefore(
                DateTime.now().subtract(
                  Duration(hours: settings['stale_hours'] as int? ?? 72),
                ),
              ) ??
              false);
      return ListTile(
        contentPadding: EdgeInsets.zero,
        leading: const Icon(Icons.menu_book_outlined, color: green),
        title: Text(book['title'] as String),
        subtitle: Text(
          '${bookMeta(book)}\n${available > 0
              ? '$available 册可借'
              : book['status'] == 'loaned'
              ? '全部借出'
              : '当前无可用复本'}${stale ? ' · 状态可能陈旧' : ''}',
        ),
        trailing: const Icon(Icons.chevron_right),
        onTap: busy ? null : () => run(() => selectBook(book)),
      );
    }),
    if (total > 15)
      Wrap(
        spacing: 8,
        runSpacing: 8,
        children: [
          button('上一页', () => search(page - 1), enabled: page > 1),
          button('下一页', () => search(page + 1), enabled: page * 15 < total),
          Padding(padding: const EdgeInsets.all(10), child: Text('第 $page 页')),
        ],
      ),
  ]);

  Widget mapView() {
    final nodes = objects(document['nodes']);
    final canUseStart = [
      for (final item in nodes.where((item) => item['kind'] == 'entrance'))
        item['id'] as String,
      if (faces.any((item) => item['id'] == actualFace)) actualFace,
    ];
    final path = route(document, start, chosenFace);
    return section('楼层导览', [
      Text(
        '${document['name'] ?? floor} · 地图 ${floorMap['revision'] ?? '未缓存'} · ${date(floorMap['updated_at'])}',
      ),
      if (nodes.isNotEmpty)
        AspectRatio(
          aspectRatio: 1.46,
          child: FloorDrawing(
            document: document,
            path: path,
            faces: faces,
            selected: chosenFace,
            onSelect: (face) => run(() => selectFace(face)),
          ),
        ),
      if (canUseStart.isNotEmpty)
        DropdownButtonFormField<String>(
          key: ValueKey('start:$start'),
          initialValue: canUseStart.contains(start) ? start : null,
          decoration: const InputDecoration(labelText: '路线起点'),
          items: canUseStart
              .map(
                (id) => DropdownMenuItem(
                  value: id,
                  child: Text(
                    id == actualFace
                        ? '上次扫码点 · $id'
                        : nodes.firstWhere((node) => node['id'] == id)['label']
                              as String,
                  ),
                ),
              )
              .toList(),
          onChanged: (value) => setState(() => start = value!),
        ),
      if (faces.isNotEmpty)
        DropdownButtonFormField<String>(
          key: ValueKey('face:$chosenFace'),
          initialValue: faces.any((item) => item['id'] == chosenFace)
              ? chosenFace
              : null,
          decoration: const InputDecoration(labelText: '目标架面'),
          items: faces
              .map(
                (face) => DropdownMenuItem(
                  value: face['id'] as String,
                  child: Text('${face['label']} · ${face['direction']}向'),
                ),
              )
              .toList(),
          onChanged: busy ? null : (value) => run(() => selectFace(value!)),
        )
      else ...[
        TextField(
          controller: manualFace,
          decoration: const InputDecoration(labelText: '手动选择架面编号'),
        ),
        button(
          '选择架面',
          () => selectFace(readMarker(manualFace.text, library, floor)),
        ),
      ],
      Text(
        chosenFace.isEmpty
            ? '请选择架面，或扫码开始。'
            : path.isEmpty
            ? '$chosenFace 暂无可通行路线，可前往$help。'
            : '${path.first['label']} → $chosenFace',
      ),
    ]);
  }

  Widget arrivalView() => section('到架确认', [
    TextField(
      controller: marker,
      decoration: const InputDecoration(
        labelText: '实际架面编号或二维码内容',
        hintText: 'A01-A',
      ),
    ),
    Wrap(
      spacing: 8,
      runSpacing: 8,
      children: [
        button('扫描架标', scanShelf, primary: true),
        button('确认架面', () => arrive(marker.text)),
        if (actualFace.isNotEmpty && chosenFace != actualFace)
          button('改用实际架面 $actualFace', () async {
            await selectFace(actualFace);
            await arrive(scannedMarker, fromScan: true);
          }),
      ],
    ),
    Text('如需协助，可前往$help。'),
  ]);

  Widget captureView() {
    final face = object(activeSession?['face'] ?? {});
    final quality = (result?['quality'] as List?) ?? [];
    final candidates = objects(result?['candidates']);
    final state = result?['state'];
    final description = {
      'unique': '定位到一本高可信候选',
      'confirm': '有待确认的候选',
      'retake': '建议重拍',
      'unrecognized': '本次未识别到目标',
    };
    return section('拍照定位', [
      Text(
        '正对层板，避开反光和遮挡。${face['direction'] == null ? '' : '当前架面朝${face['direction']}。'}',
      ),
      DropdownButtonFormField<String>(
        initialValue: capture,
        isExpanded: true,
        decoration: const InputDecoration(labelText: '取景范围'),
        items: const [
          DropdownMenuItem(value: 'partial', child: Text('局部照片 · 按画面左起')),
          DropdownMenuItem(value: 'left_visible', child: Text('左边界可见 · 按本层左起')),
        ],
        onChanged: busy
            ? null
            : (value) => setState(() {
                capture = value!;
                result = null;
              }),
      ),
      Wrap(
        spacing: 8,
        runSpacing: 8,
        children: [
          button('拍照 / 重拍', () => pick(ImageSource.camera), primary: true),
          button('选择照片', () => pick(ImageSource.gallery)),
          if (photo != null) button('本机识别', recognizePhoto),
        ],
      ),
      if (photo != null)
        ClipRRect(
          borderRadius: BorderRadius.circular(10),
          child: SpinePhoto(
            path: photoPreview ?? photo!,
            candidates: candidates,
            unique: state == 'unique',
          ),
        ),
      if (photo != null && settings['cloud_review'] == true) ...[
        CheckboxListTile(
          contentPadding: EdgeInsets.zero,
          value: consent,
          onChanged: busy
              ? null
              : (value) => setState(() => consent = value ?? false),
          title: Text(
            settings['photo_retention_days'] == 0
                ? '同意上传本次照片用于云端复核，识别后删除'
                : '同意上传本次照片用于云端复核，最长保存 ${settings['photo_retention_days']} 天',
          ),
        ),
        if (consent)
          button(
            '上传本次照片进行复核',
            () => recognizePhoto(review: true),
            enabled: online,
          ),
      ],
      if (result != null) ...[
        Text(
          description[state] ?? '',
          style: Theme.of(context).textTheme.titleMedium,
        ),
        ...quality.map((text) => Text(text as String)),
        for (final (index, item) in candidates.indexed)
          ListTile(
            contentPadding: EdgeInsets.zero,
            title: Text(
              '${index + 1} 号框 · 画面第 ${item['row']} 排，${item['column_scope'] == 'shelf_row' ? '本层' : '画面'}左起第 ${item['column']} 本',
            ),
            subtitle: Text(
              '读到 ${(item['text'] as String).replaceAll('\n', ' · ')}\n${(item['evidence'] as List).join('、')}'
              '${objects(item['alternatives']).isEmpty ? '' : '\n可比对 ${objects(item['alternatives']).map((book) => '${book['title']} ${bookMeta(book)}').join('；')}'}',
            ),
          ),
        Text(
          '${result!['model_version']} · ${result!['source']} · ${result!['device_ms'] ?? result!['duration_ms']} 毫秒',
        ),
      ],
      button('核验取下的书', () async {
        goToStep(3);
      }, enabled: activeSession != null),
    ]);
  }

  Widget verificationView() {
    final current = object(verification?['current'] ?? {});
    const labels = {'correct': '条码核对正确', 'wrong': '版本或复本不符', 'pending': '待核验'};
    return section('取书核验', [
      TextField(
        controller: barcode,
        decoration: const InputDecoration(labelText: '实物馆藏条码'),
      ),
      Wrap(
        spacing: 8,
        runSpacing: 8,
        children: [
          button('核对条码', () => verify(barcode.text.trim()), primary: true),
          button('扫码核对', () async {
            final value = await scan(ImageSource.camera);
            if (value != null) {
              barcode.text = value;
              await verify(value);
            }
          }),
          button('没有可用条码', () => verify(null)),
          button('继续拍照查找', () async {
            goToStep(2);
          }),
        ],
      ),
      if (current.isNotEmpty) ...[
        Text(
          labels[current['outcome']] ?? '待核验',
          style: Theme.of(context).textTheme.titleMedium,
        ),
        if (current['expected'] is Map)
          Text(
            '目标 ${current['expected']['title']} · ${bookMeta(object(current['expected']))}',
          ),
        if (current['actual'] is Map)
          Text(
            '实物 ${current['actual']['title']} · ${bookMeta(object(current['actual']))} · ${current['holding_id']}',
          ),
        Text(
          '首拿 ${labels[verification?['first']['outcome']]} · 最终 ${verification?['final'] == null ? '待核验' : '核对正确'}',
        ),
      ],
      if (demo) const Text('第 3 版演示条码 DIW000001／DIW000009，第 2 版 DIW000002。'),
    ]);
  }

  Widget feedbackView() => section('反馈与核查', [
    DropdownButtonFormField<String>(
      key: ValueKey(reason),
      initialValue: reason,
      isExpanded: true,
      decoration: const InputDecoration(labelText: '反馈原因'),
      items: const [
        DropdownMenuItem(value: 'unrecognized', child: Text('本次未识别')),
        DropdownMenuItem(value: 'retake', child: Text('连续重拍')),
        DropdownMenuItem(value: 'wrong_edition', child: Text('版本或复本不符')),
        DropdownMenuItem(value: 'marker_damage', child: Text('架标损坏')),
        DropdownMenuItem(value: 'suspected_misplaced', child: Text('疑似错架')),
        DropdownMenuItem(value: 'found', child: Text('我找到了（自报）')),
      ],
      onChanged: busy ? null : (value) => setState(() => reason = value!),
    ),
    TextField(
      controller: note,
      decoration: const InputDecoration(labelText: '情况说明（选填）'),
      maxLines: 2,
      maxLength: 2000,
    ),
    Wrap(
      spacing: 8,
      runSpacing: 8,
      children: [
        button('提交反馈', feedback, primary: true),
        if (workorder != null)
          button('刷新回执', () async {
            receipt = await pilot.request(
              '/workorders/$workorder?session_id=${activeSession!['session_id']}',
            );
            await saveTask();
          }),
      ],
    ),
    if (workorder != null) Text('工单 $workorder'),
    if (receipt?['finding'] != null && receipt!['finding'] != '')
      Text(
        const {
              'on_shelf': '馆员已核实在架',
              'misplaced': '馆员已核实位置异常',
              'marker_damaged': '馆员已核实架标损坏',
              'unresolved': '核查后仍无法判断',
            }[receipt!['finding']] ??
            '',
      ),
    if ((receipt?['receipt'] as String? ?? '').isNotEmpty)
      Text(receipt!['receipt'] as String),
  ]);

  @override
  Widget build(BuildContext context) {
    const names = ['查书选版', '到架', '拍照定位', '取书核验'];
    final live = online
        ? (demo ? '演示馆 · 在线' : '馆内服务在线')
        : '离线 · 本地书目 ${date(catalog['updated_at'])}';
    return MediaQuery(
      data: MediaQuery.of(context).copyWith(
        textScaler: large
            ? const TextScaler.linear(1.4)
            : MediaQuery.textScalerOf(context),
      ),
      child: Scaffold(
        appBar: AppBar(
          title: const Text('数智文枢'),
          actions: [
            IconButton(
              tooltip: '切换大字模式',
              onPressed: () => setState(() => large = !large),
              icon: const Icon(Icons.text_fields),
            ),
            IconButton(
              tooltip: '服务地址',
              onPressed: busy ? null : () => run(configure),
              icon: const Icon(Icons.settings_outlined),
            ),
          ],
        ),
        body: SafeArea(
          child: ListView(
            controller: scroll,
            padding: const EdgeInsets.all(12),
            children: [
              Text('$live${queue.isEmpty ? '' : ' · ${queue.length} 项待同步'}'),
              if (busy) const LinearProgressIndicator(),
              if (notice.isNotEmpty)
                Semantics(
                  liveRegion: true,
                  child: Padding(
                    padding: const EdgeInsets.symmetric(vertical: 10),
                    child: Text(notice),
                  ),
                ),
              Wrap(
                spacing: 6,
                runSpacing: 5,
                children: [
                  for (final (index, label) in names.indexed)
                    ChoiceChip(
                      label: Text(label),
                      selected: step == index,
                      onSelected:
                          busy ||
                              index > 1 && activeSession == null ||
                              index == 1 && selected == null
                          ? null
                          : (_) => setState(() => goToStep(index)),
                    ),
                ],
              ),
              if (!online) button('重新连接并更新缓存', refresh),
              if (selected != null)
                section('当前任务', [
                  Text('${selected!['title']} · ${bookMeta(selected!)}'),
                  Text(holdingId == null ? '同版任一可用复本' : '指定复本 $holdingId'),
                  if (activeSession != null)
                    SelectableText('会话号 ${activeSession!['session_id']}'),
                ]),
              if (step == 0) catalogView(),
              if (step == 1) ...[mapView(), arrivalView()],
              if (step == 2) captureView(),
              if (step == 3) verificationView(),
              if (activeSession != null && step >= 2) feedbackView(),
            ],
          ),
        ),
      ),
    );
  }
}

class FloorDrawing extends StatelessWidget {
  const FloorDrawing({
    super.key,
    required this.document,
    required this.path,
    required this.faces,
    required this.selected,
    required this.onSelect,
  });
  final Json document;
  final List<Json> path, faces;
  final String selected;
  final ValueChanged<String> onSelect;

  @override
  Widget build(BuildContext context) {
    final encoded = document['image'] as String?;
    final nodes = {
      for (final item in objects(document['nodes'])) item['id']: item,
    };
    return LayoutBuilder(
      builder: (context, constraints) => ClipRRect(
        borderRadius: BorderRadius.circular(12),
        child: ColoredBox(
          color: const Color(0xffedf3eb),
          child: Stack(
            fit: StackFit.expand,
            children: [
              if (encoded != null)
                Image.memory(base64Decode(encoded), fit: BoxFit.fill),
              CustomPaint(painter: MapPainter(document, path)),
              for (final face in faces)
                if (nodes.containsKey(face['node_id']))
                  Positioned(
                    left:
                        ((nodes[face['node_id']]!['x'] as num) /
                                    100 *
                                    constraints.maxWidth -
                                33)
                            .clamp(0.0, constraints.maxWidth - 66)
                            .toDouble(),
                    top:
                        ((nodes[face['node_id']]!['y'] as num) /
                                    100 *
                                    constraints.maxHeight -
                                19)
                            .clamp(0.0, constraints.maxHeight - 38)
                            .toDouble(),
                    width: 66,
                    height: 38,
                    child: Semantics(
                      button: true,
                      label: face['id'] as String,
                      child: InkWell(
                        onTap: () => onSelect(face['id'] as String),
                        child: Center(
                          child: Container(
                            padding: const EdgeInsets.symmetric(
                              horizontal: 4,
                              vertical: 2,
                            ),
                            decoration: BoxDecoration(
                              color: selected == face['id']
                                  ? const Color(0xffffe2c1)
                                  : Colors.white,
                              borderRadius: BorderRadius.circular(5),
                            ),
                            child: FittedBox(
                              fit: BoxFit.scaleDown,
                              child: Text(
                                face['id'] as String,
                                style: const TextStyle(
                                  fontWeight: FontWeight.bold,
                                  fontSize: 10,
                                ),
                              ),
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
            ],
          ),
        ),
      ),
    );
  }
}

class MapPainter extends CustomPainter {
  MapPainter(this.document, this.route);
  final Json document;
  final List<Json> route;
  @override
  void paint(Canvas canvas, Size size) {
    Offset location(Json item) => Offset(
      (item['x'] as num) / 100 * size.width,
      (item['y'] as num) / 100 * size.height,
    );
    final nodes = {
      for (final item in objects(document['nodes'])) item['id']: item,
    };
    for (final block in objects(document['blocks'])) {
      final bounds =
          location(block) &
          Size(
            (block['width'] as num) / 100 * size.width,
            (block['height'] as num) / 100 * size.height,
          );
      canvas.drawRRect(
        RRect.fromRectAndRadius(bounds, const Radius.circular(5)),
        Paint()..color = const Color(0xffd4e6d6),
      );
    }
    for (final edge in objects(document['edges'])) {
      if (edge['accessible'] == true &&
          nodes.containsKey(edge['a']) &&
          nodes.containsKey(edge['b'])) {
        canvas.drawLine(
          location(nodes[edge['a']]!),
          location(nodes[edge['b']]!),
          Paint()
            ..color = const Color(0xffadbdaf)
            ..strokeWidth = 1,
        );
      }
    }
    for (var i = 1; i < route.length; i++) {
      canvas.drawLine(
        location(route[i - 1]),
        location(route[i]),
        Paint()
          ..color = green
          ..strokeWidth = 4
          ..strokeCap = StrokeCap.round,
      );
    }
    for (final item in nodes.values.where(
      (item) => item['kind'] == 'entrance' || item['kind'] == 'service',
    )) {
      final pos = location(item);
      canvas.drawCircle(pos, 4, Paint()..color = const Color(0xff466d98));
      final caption = TextPainter(
        text: TextSpan(
          text: item['label'] as String,
          style: const TextStyle(color: Color(0xff23392e), fontSize: 10),
        ),
        textDirection: TextDirection.ltr,
      )..layout();
      caption.paint(canvas, Offset(pos.dx - caption.width / 2, pos.dy + 6));
    }
  }

  @override
  bool shouldRepaint(covariant MapPainter oldDelegate) => true;
}

class SpinePhoto extends StatelessWidget {
  const SpinePhoto({
    super.key,
    required this.path,
    required this.candidates,
    required this.unique,
  });
  final String path;
  final List<Json> candidates;
  final bool unique;

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, constraints) => Stack(
      children: [
        Image.file(
          File(path),
          width: constraints.maxWidth,
          fit: BoxFit.fitWidth,
          errorBuilder: (_, __, ___) => const Padding(
            padding: EdgeInsets.all(12),
            child: Text('图片已不可用，请重拍。'),
          ),
        ),
        if (candidates.isNotEmpty)
          Positioned.fill(
            child: IgnorePointer(
              child: CustomPaint(painter: SpinePainter(candidates, unique)),
            ),
          ),
      ],
    ),
  );
}

class SpinePainter extends CustomPainter {
  SpinePainter(this.candidates, this.unique);
  final List<Json> candidates;
  final bool unique;

  @override
  void paint(Canvas canvas, Size size) {
    final color = unique ? green : const Color(0xff85501a);
    for (final (index, item) in candidates.indexed) {
      final quad = points(item)
          .map((point) => Offset(point[0] * size.width, point[1] * size.height))
          .toList();
      final path = Path()..addPolygon(quad, true);
      canvas.drawPath(path, Paint()..color = color.withValues(alpha: .25));
      canvas.drawPath(
        path,
        Paint()
          ..color = color
          ..style = PaintingStyle.stroke
          ..strokeWidth = 3,
      );
      final label = TextPainter(
        text: TextSpan(
          text: '${index + 1}',
          style: const TextStyle(
            color: Colors.white,
            fontSize: 15,
            fontWeight: FontWeight.bold,
          ),
        ),
        textDirection: TextDirection.ltr,
      )..layout();
      final anchor = quad.first + const Offset(3, 3);
      canvas.drawRRect(
        RRect.fromRectAndRadius(
          anchor & Size(label.width + 10, label.height + 5),
          const Radius.circular(4),
        ),
        Paint()..color = color,
      );
      label.paint(canvas, anchor + const Offset(5, 2));
    }
  }

  @override
  bool shouldRepaint(covariant SpinePainter oldDelegate) => true;
}
