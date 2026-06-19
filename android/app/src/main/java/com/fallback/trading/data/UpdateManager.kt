package com.fallback.trading.data

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import com.fallback.trading.BuildConfig
import com.squareup.moshi.Moshi
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Resolved "latest release" info the UI needs. */
data class ReleaseInfo(
    val versionName: String,   // tag without a leading "v"
    val tag: String,
    val notes: String,
    val apkUrl: String,
    val htmlUrl: String,
    val sizeBytes: Long,
)

/**
 * Self-update via GitHub Releases. Checks the public repo's latest release,
 * compares it to the installed [BuildConfig.VERSION_NAME], downloads the signed
 * APK asset and hands it to the system package installer.
 *
 * For an in-app update to install over the current app, every release must be
 * signed with the same key — see .github/workflows/android-release.yml.
 */
class UpdateManager(
    private val context: Context,
    moshi: Moshi,
) {
    // Separate client from the API one: this one *follows* redirects (GitHub asset
    // URLs redirect to object storage) and needs no cookies/CSRF.
    private val client = OkHttpClient.Builder()
        .followRedirects(true)
        .followSslRedirects(true)
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    private val releaseAdapter = moshi.adapter(ReleaseDto::class.java)

    private val latestUrl =
        "https://api.github.com/repos/${BuildConfig.GITHUB_OWNER}/${BuildConfig.GITHUB_REPO}/releases/latest"

    suspend fun fetchLatest(): ReleaseInfo? = withContext(Dispatchers.IO) {
        val request = Request.Builder()
            .url(latestUrl)
            .header("Accept", "application/vnd.github+json")
            .build()
        client.newCall(request).execute().use { resp ->
            if (!resp.isSuccessful) return@withContext null
            val body = resp.body?.string() ?: return@withContext null
            val dto = runCatching { releaseAdapter.fromJson(body) }.getOrNull() ?: return@withContext null
            val apk = dto.assets.firstOrNull { it.name.endsWith(".apk", ignoreCase = true) }
                ?: return@withContext null
            ReleaseInfo(
                versionName = dto.tagName.removePrefix("v").trim(),
                tag = dto.tagName,
                notes = dto.body?.trim().orEmpty(),
                apkUrl = apk.browserDownloadUrl,
                htmlUrl = dto.htmlUrl.orEmpty(),
                sizeBytes = apk.size,
            )
        }
    }

    fun isNewer(remoteVersionName: String): Boolean =
        compareVersions(remoteVersionName, BuildConfig.VERSION_NAME) > 0

    /** Streams the APK to the cache dir, reporting 0f..1f progress. */
    suspend fun download(info: ReleaseInfo, onProgress: (Float) -> Unit): File =
        withContext(Dispatchers.IO) {
            val request = Request.Builder().url(info.apkUrl).build()
            client.newCall(request).execute().use { resp ->
                if (!resp.isSuccessful) throw IOException("Download failed (HTTP ${resp.code})")
                val responseBody = resp.body ?: throw IOException("Empty download")
                val total = if (info.sizeBytes > 0) info.sizeBytes else responseBody.contentLength()
                val dir = File(context.cacheDir, "updates").apply { mkdirs() }
                val outFile = File(dir, "fallback-${info.tag}.apk")
                responseBody.byteStream().use { input ->
                    outFile.outputStream().use { output ->
                        val buffer = ByteArray(16 * 1024)
                        var downloaded = 0L
                        while (true) {
                            val read = input.read(buffer)
                            if (read == -1) break
                            output.write(buffer, 0, read)
                            downloaded += read
                            if (total > 0) onProgress((downloaded.toFloat() / total).coerceIn(0f, 1f))
                        }
                    }
                }
                outFile
            }
        }

    fun install(file: File) {
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(intent)
    }

    /** Android 8+ requires per-app permission to install APKs. */
    fun canInstall(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.O || context.packageManager.canRequestPackageInstalls()

    fun requestInstallPermission() {
        val intent = Intent(
            Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
            Uri.parse("package:${context.packageName}"),
        ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(intent)
    }

    private companion object {
        /** Returns >0 if [a] is a higher semantic version than [b]. */
        fun compareVersions(a: String, b: String): Int {
            val pa = a.split('.', '-').mapNotNull { it.toIntOrNull() }
            val pb = b.split('.', '-').mapNotNull { it.toIntOrNull() }
            val n = maxOf(pa.size, pb.size)
            for (i in 0 until n) {
                val x = pa.getOrElse(i) { 0 }
                val y = pb.getOrElse(i) { 0 }
                if (x != y) return x - y
            }
            return 0
        }
    }
}
