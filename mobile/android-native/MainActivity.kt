package cn.diw.diw_reader

import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.Matrix
import android.os.Build
import androidx.exifinterface.media.ExifInterface
import com.google.android.gms.tasks.Tasks
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.chinese.ChineseTextRecognizerOptions
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.File
import java.util.concurrent.Executors
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

class MainActivity : FlutterActivity() {
    private val executor = Executors.newSingleThreadExecutor()
    private val reader by lazy { TextRecognition.getClient(ChineseTextRecognizerOptions.Builder().build()) }
    private val scanner by lazy { BarcodeScanning.getClient() }
    private var channel: MethodChannel? = null

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        if (intent.data?.scheme == "diw" && intent.data?.host == "shelf") {
            channel?.invokeMethod("markerAvailable", null)
        }
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        channel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "cn.diw.reader/vision")
        channel?.setMethodCallHandler { call, result ->
                if (call.method == "readMarkerIntent") {
                    val data = intent.data
                    if (data?.scheme == "diw" && data.host == "shelf") {
                        intent.setData(null)
                        result.success(data.toString())
                    } else {
                        result.success(null)
                    }
                    return@setMethodCallHandler
                }
                val path = call.argument<String>("path")
                if (call.method !in listOf("recognize", "barcode") || path == null) {
                    result.notImplemented()
                } else {
                    executor.execute {
                        try {
                            val bitmap = loadBitmap(path, if (call.method == "barcode") 1800 else 1350)
                            val value = if (call.method == "barcode") {
                                Tasks.await(scanner.process(InputImage.fromBitmap(bitmap, 0)))
                                    .mapNotNull { it.rawValue }.distinct()
                            } else {
                                recognize(bitmap)
                            }
                            runOnUiThread { result.success(value) }
                        } catch (error: Exception) {
                            runOnUiThread { result.error("VISION", error.message ?: "无法读取照片", null) }
                        }
                    }
                }
            }
    }

    private fun loadBitmap(path: String, limit: Int): Bitmap {
        val probe = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(path, probe)
        require(probe.outWidth > 0 && probe.outHeight > 0) { "照片无法读取" }
        val options = BitmapFactory.Options().apply {
            inPreferredConfig = Bitmap.Config.ARGB_8888
            inSampleSize = 1
            while (max(probe.outWidth, probe.outHeight) / (inSampleSize * 2) > limit) inSampleSize *= 2
        }
        val source = requireNotNull(BitmapFactory.decodeFile(path, options)) { "照片无法读取" }
        val exif = ExifInterface(path)
        val rotation = Matrix()
        if (exif.isFlipped) rotation.postScale(-1f, 1f)
        rotation.postRotate(exif.rotationDegrees.toFloat())
        val scale = min(1f, limit.toFloat() / max(source.width, source.height))
        rotation.postScale(scale, scale)
        return Bitmap.createBitmap(source, 0, 0, source.width, source.height, rotation, true)
    }

    private fun recognize(bitmap: Bitmap): Map<String, Any> {
        val width = bitmap.width
        val height = bitmap.height
        val file = File.createTempFile("shelf-", ".jpg", cacheDir)
        try {
            file.outputStream().use { bitmap.compress(Bitmap.CompressFormat.JPEG, 90, it) }
            val pixels = IntArray(width * height)
            bitmap.getPixels(pixels, 0, width, 0, 0, width, height)
            val flags = quality(pixels, width, height).toMutableList()
            val boxes = if (flags.isEmpty()) segments(pixels, width, height) else emptyList()
            if (flags.isEmpty() && boxes.isEmpty()) flags.add("未检测到书脊，请正对层板重新取景")
            val observations = boxes.take(80).map { box ->
                val (left, top, right, bottom) = box
                val crop = Bitmap.createBitmap(bitmap, left, top, right-left, bottom-top)
                val orientations = if (crop.height > crop.width * 1.10) listOf(0, 90, 270) else listOf(0)
                fun read(rotation: Int): Triple<Double, String, Int> {
                    val text = Tasks.await(reader.process(InputImage.fromBitmap(crop, rotation)))
                    val lines = text.textBlocks.flatMap { it.lines }.filter { it.text.isNotBlank() }
                    val score = lines.sumOf { line ->
                        line.text.length * (line.confidence.takeIf { it > 0f }?.toDouble() ?: .75)
                    }
                    return Triple(score, lines.joinToString("\n") { it.text }.take(400), rotation)
                }
                val horizontal = read(0)
                val recognized = if (orientations.size == 1 || (horizontal.first >= 10 && horizontal.second.split("\n").count { it.length >= 3 } >= 2)) {
                    horizontal
                } else {
                    (listOf(horizontal) + orientations.drop(1).map(::read)).maxByOrNull { it.first }!!
                }
                val x0 = left.toDouble()/width
                val x1 = right.toDouble()/width
                val y0 = top.toDouble()/height
                val y1 = bottom.toDouble()/height
                mapOf(
                    "text" to recognized.second,
                    "direction" to if (recognized.third == 0) "horizontal" else "vertical",
                    "quad" to listOf(listOf(x0,y0), listOf(x1,y0), listOf(x1,y1), listOf(x0,y1))
                )
            }
            return mapOf("path" to file.absolutePath, "quality" to flags, "observations" to observations,
                         "device_model" to "${Build.MANUFACTURER} ${Build.MODEL}".take(80))
        } catch (error: Exception) {
            file.delete()
            throw error
        }
    }

    private fun light(pixel: Int): Double =
        .299 * Color.red(pixel) + .587 * Color.green(pixel) + .114 * Color.blue(pixel)

    private fun quality(pixels: IntArray, width: Int, height: Int): List<String> {
        if (min(width, height) < 275) return listOf("分辨率过低，请靠近书架重新拍照")
        var sum = 0.0
        var square = 0.0
        var mean = 0.0
        var bright = 0
        var count = 0
        for (y in 2 until height-2 step 2) for (x in 2 until width-2 step 2) {
            val p = y*width+x
            val level = light(pixels[p])
            val edge = light(pixels[p-2]) + light(pixels[p+2]) + light(pixels[p-2*width]) +
                light(pixels[p+2*width]) - 4*level
            sum += edge
            square += edge*edge
            mean += level
            if (level >= 249) bright++
            count++
        }
        return buildList {
            if (square/count - (sum/count)*(sum/count) < 22) add("画面模糊，请稳住手机重拍")
            if (bright.toDouble()/count > .42) add("画面有明显反光或过曝，请调整拍摄角度")
            if (mean/count < 27) add("画面过暗，请调整光照")
        }
    }

    private fun colorDistance(a: Int, b: Int): Int = maxOf(
        abs(Color.red(a)-Color.red(b)), abs(Color.green(a)-Color.green(b)), abs(Color.blue(a)-Color.blue(b))
    )

    private fun peaks(counts: DoubleArray, threshold: Double, gap: Int): List<Int> {
        val result = mutableListOf<Int>()
        var previous = -gap-1
        for (i in counts.indices) if (counts[i] > threshold) {
            if (i-previous > gap) result.add(i)
            else if (counts[i] > counts[result.last()]) result[result.lastIndex] = i
            previous = i
        }
        return result
    }

    private fun segments(pixels: IntArray, width: Int, height: Int): List<List<Int>> {
        val yEdges = DoubleArray(height)
        for (y in 2 until height-2) {
            var edges = 0
            for (x in 0 until width step 3) {
                if (colorDistance(pixels[(y-2)*width+x], pixels[(y+2)*width+x]) > 24) edges++
            }
            yEdges[y] = 3.0 * edges / width
        }
        val rows = listOf(0) + peaks(yEdges, .62, max(9, height/28)).filter { it > height*.13 && it < height*.87 } + height
        val boxes = mutableListOf<List<Int>>()
        for ((top,bottom) in rows.zipWithNext()) {
            val size = bottom-top
            if (size < height*.18) continue
            val xEdges = DoubleArray(width)
            for (x in 2 until width-2) {
                var edges = 0
                val trim = size/12
                for (y in (top+trim) until (bottom-trim) step 3) {
                    if (colorDistance(pixels[y*width+x-2], pixels[y*width+x+2]) > 23) edges++
                }
                xEdges[x] = 3.0 * edges / (size-2*trim)
            }
            val columns = listOf(0) + peaks(xEdges, .49, max(5, width/85)).filter { it > width*.018 && it < width*.982 } + width
            for ((left,right) in columns.zipWithNext()) {
                if (right-left >= max(16.0,width*.019) && right-left <= min(width*.46,size*1.65)) {
                    boxes.add(listOf(left,top,right,bottom))
                }
            }
        }
        return boxes
    }
}
