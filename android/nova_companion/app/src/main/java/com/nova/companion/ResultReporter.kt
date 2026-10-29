package com.nova.companion

import android.content.Context
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.URL

/**
 * ResultReporter — submits command results back to the Nova backend.
 *
 * Endpoint: POST /device/result
 * Auth:     X-Device-Id + X-Device-Token headers
 * Body:     {"command_id", "status", "detail", "data"}
 *
 * Design:
 * - Uses plain HttpURLConnection (no Retrofit/OkHttp dependency required for skeleton)
 * - In a real implementation, replace with OkHttp or Retrofit for better error handling
 * - If submission fails (network error), logs the error but does NOT retry infinitely
 *   (the command will time out on the server side)
 */
class ResultReporter(private val context: Context) {

    companion object {
        private const val TAG     = "ResultReporter"
        private const val TIMEOUT = 10_000  // 10 seconds
    }

    /**
     * Submit the result of a completed command to the backend.
     *
     * @param commandId  The command_id from the original command envelope
     * @param result     The CommandResult from ActionDispatcher / CommandReceiver
     */
    suspend fun submit(commandId: String, result: CommandResult) {
        withContext(Dispatchers.IO) {
            val credentials = CredentialStore.load(context)
            val deviceId    = credentials.deviceId ?: run {
                Log.e(TAG, "Cannot submit result — no device_id stored")
                return@withContext
            }
            val token = credentials.token ?: run {
                Log.e(TAG, "Cannot submit result — no token stored")
                return@withContext
            }
            val baseUrl = credentials.baseUrl ?: run {
                Log.e(TAG, "Cannot submit result — no base_url stored")
                return@withContext
            }

            val body = JSONObject().apply {
                put("command_id", commandId)
                put("status",     result.status)
                put("detail",     result.detail)
                put("data",       JSONObject(result.data.mapValues { it.value.toString() }))
            }

            Log.i(TAG, "Submitting result: cmd=${commandId.take(8)} status=${result.status}")

            try {
                val url  = URL("$baseUrl/device/result")
                val conn = (url.openConnection() as HttpURLConnection).apply {
                    requestMethod      = "POST"
                    connectTimeout     = TIMEOUT
                    readTimeout        = TIMEOUT
                    doOutput           = true
                    setRequestProperty("Content-Type",    "application/json")
                    setRequestProperty("X-Device-Id",     deviceId)
                    setRequestProperty("X-Device-Token",  token)
                }

                OutputStreamWriter(conn.outputStream).use { writer ->
                    writer.write(body.toString())
                    writer.flush()
                }

                val responseCode = conn.responseCode
                if (responseCode == 200) {
                    Log.i(TAG, "Result submitted OK: cmd=${commandId.take(8)}")
                } else {
                    Log.w(TAG, "Result submit failed: HTTP $responseCode for cmd=${commandId.take(8)}")
                }
                conn.disconnect()

            } catch (e: Exception) {
                Log.e(TAG, "Result submit network error: ${e.message}", e)
                // The web UI will see no result for this command_id
                // and the server will eventually time it out
            }
        }
    }
}

/**
 * CredentialStore — thin wrapper around EncryptedSharedPreferences.
 *
 * Stores: device_id, token, base_url
 * In a real implementation, use androidx.security.crypto.EncryptedSharedPreferences
 * to protect the token at rest.
 */
object CredentialStore {

    private const val PREFS_FILE   = "nova_credentials"
    private const val KEY_DEVICE_ID = "device_id"
    private const val KEY_TOKEN     = "token"
    private const val KEY_BASE_URL  = "base_url"

    data class Credentials(
        val deviceId: String?,
        val token:    String?,
        val baseUrl:  String?,
    )

    fun load(context: Context): Credentials {
        // TODO: replace with EncryptedSharedPreferences in production
        val prefs = context.getSharedPreferences(PREFS_FILE, Context.MODE_PRIVATE)
        return Credentials(
            deviceId = prefs.getString(KEY_DEVICE_ID, null),
            token    = prefs.getString(KEY_TOKEN,     null),
            baseUrl  = prefs.getString(KEY_BASE_URL,  "http://10.0.2.2:8000"),
            // 10.0.2.2 = localhost from Android emulator
        )
    }

    fun save(context: Context, deviceId: String, token: String, baseUrl: String) {
        context.getSharedPreferences(PREFS_FILE, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_DEVICE_ID, deviceId)
            .putString(KEY_TOKEN,     token)
            .putString(KEY_BASE_URL,  baseUrl)
            .apply()
    }

    fun clearToken(context: Context) {
        context.getSharedPreferences(PREFS_FILE, Context.MODE_PRIVATE)
            .edit()
            .remove(KEY_TOKEN)
            .apply()
    }
}

/**
 * Minimal HTTP client used by NovaService for polling.
 * Replace with OkHttp in a real implementation.
 */
object HttpClient {

    data class Response(val statusCode: Int, val body: String)

    fun get(path: String, headers: Map<String, String> = emptyMap()): Response {
        // baseUrl is loaded per-call; in production inject via DI
        throw NotImplementedError(
            "HttpClient.get() is a skeleton. " +
            "Replace with OkHttp: client.newCall(request).execute()"
        )
    }
}
