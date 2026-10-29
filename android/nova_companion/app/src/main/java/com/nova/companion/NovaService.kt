package com.nova.companion

import android.app.*
import android.content.Intent
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.*
import org.json.JSONObject

/**
 * NovaService — Foreground service that maintains the polling loop.
 *
 * Lifecycle:
 *   MainActivity.startService() → NovaService starts
 *   NovaService creates a persistent notification (required for foreground)
 *   Coroutine polls GET /device/poll every POLL_INTERVAL_MS milliseconds
 *   Each received command is passed to CommandReceiver
 *   ResultReporter posts the result back to /device/result
 *
 * The service is started on boot via BootReceiver.
 */
class NovaService : Service() {

    companion object {
        private const val TAG              = "NovaService"
        private const val CHANNEL_ID       = "nova_service_channel"
        private const val NOTIFICATION_ID  = 1001
        private const val POLL_INTERVAL_MS = 3_000L  // 3 seconds

        // Broadcast action sent to MainActivity when a result arrives
        const val ACTION_RESULT_RECEIVED = "com.nova.companion.RESULT_RECEIVED"
    }

    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    private lateinit var commandReceiver: CommandReceiver
    private lateinit var resultReporter:  ResultReporter

    // ---------------------------------------------------------------------------
    // Service lifecycle
    // ---------------------------------------------------------------------------

    override fun onCreate() {
        super.onCreate()
        Log.i(TAG, "Service created")

        commandReceiver = CommandReceiver(applicationContext)
        resultReporter  = ResultReporter(applicationContext)

        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Log.i(TAG, "Service starting")
        startForeground(NOTIFICATION_ID, buildNotification("Connecting…"))
        startPolling()
        return START_STICKY   // restart automatically if killed
    }

    override fun onDestroy() {
        super.onDestroy()
        serviceScope.cancel()
        Log.i(TAG, "Service destroyed")
    }

    override fun onBind(intent: Intent?): IBinder? = null

    // ---------------------------------------------------------------------------
    // Polling loop
    // ---------------------------------------------------------------------------

    private fun startPolling() {
        serviceScope.launch {
            Log.i(TAG, "Polling loop started (interval=${POLL_INTERVAL_MS}ms)")
            updateNotification("Connected — listening for commands")

            while (isActive) {
                try {
                    poll()
                } catch (e: CancellationException) {
                    throw e    // don't swallow cancellation
                } catch (e: Exception) {
                    Log.e(TAG, "Poll error: ${e.message}", e)
                    updateNotification("Connection error — retrying")
                }
                delay(POLL_INTERVAL_MS)
            }
        }
    }

    private suspend fun poll() {
        val prefs     = CredentialStore.load(applicationContext)
        val deviceId  = prefs.deviceId  ?: return
        val token     = prefs.token     ?: return

        // GET /device/poll  with X-Device-Id + X-Device-Token
        val response = HttpClient.get(
            path    = "/device/poll",
            headers = mapOf(
                "X-Device-Id"    to deviceId,
                "X-Device-Token" to token,
            )
        )

        when (response.statusCode) {
            200 -> {
                val body     = JSONObject(response.body)
                val commands = body.getJSONArray("commands")
                Log.d(TAG, "Poll: ${commands.length()} command(s) received")

                for (i in 0 until commands.length()) {
                    val cmd = commands.getJSONObject(i)
                    // Process each command in its own coroutine so one slow
                    // command doesn't block the others
                    launch { handleCommand(cmd) }
                }
            }
            401 -> {
                Log.w(TAG, "Poll: 401 — token invalid, triggering re-registration")
                CredentialStore.clearToken(applicationContext)
                updateNotification("Session expired — please re-register")
            }
            else -> {
                Log.w(TAG, "Poll: unexpected status ${response.statusCode}")
            }
        }
    }

    private suspend fun handleCommand(cmd: JSONObject) {
        val commandId = cmd.getString("command_id")
        val intent    = cmd.getString("intent")
        val params    = cmd.getJSONObject("parameters")
        val timeoutMs = cmd.optLong("timeout_s", 30) * 1000L

        Log.i(TAG, "Handling command: intent=$intent  id=${commandId.take(8)}")
        updateNotification("Running: $intent")

        val result = withTimeoutOrNull(timeoutMs) {
            commandReceiver.handle(intent, params)
        } ?: CommandResult(
            status = "failed",
            detail = "Command timed out after ${timeoutMs / 1000}s",
        )

        resultReporter.submit(commandId, result)
        updateNotification("Connected — listening for commands")

        // Optionally notify MainActivity for UI update
        sendBroadcast(Intent(ACTION_RESULT_RECEIVED).apply {
            putExtra("command_id", commandId)
            putExtra("status",     result.status)
        })
    }

    // ---------------------------------------------------------------------------
    // Notification helpers
    // ---------------------------------------------------------------------------

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Nova Assistant Service",
            NotificationManager.IMPORTANCE_LOW,    // silent — no sound/vibration
        ).apply {
            description = "Keeps Nova Companion connected to your assistant"
        }
        getSystemService(NotificationManager::class.java)
            .createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        val openAppIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_nova_notification)
            .setContentTitle("Nova Assistant")
            .setContentText(text)
            .setContentIntent(openAppIntent)
            .setOngoing(true)
            .setSilent(true)
            .build()
    }

    private fun updateNotification(text: String) {
        val mgr = getSystemService(NotificationManager::class.java)
        mgr.notify(NOTIFICATION_ID, buildNotification(text))
    }
}

// ---------------------------------------------------------------------------
// Boot receiver — restarts the service after device reboot
// ---------------------------------------------------------------------------
class BootReceiver : android.content.BroadcastReceiver() {
    override fun onReceive(context: android.content.Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED ||
            intent.action == Intent.ACTION_MY_PACKAGE_REPLACED) {
            val credentials = CredentialStore.load(context)
            if (credentials.deviceId != null && credentials.token != null) {
                context.startForegroundService(Intent(context, NovaService::class.java))
            }
        }
    }
}
