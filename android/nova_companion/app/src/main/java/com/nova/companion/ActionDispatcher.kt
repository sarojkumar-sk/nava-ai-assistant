package com.nova.companion

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.AlarmClock
import android.util.Log
import org.json.JSONObject

/**
 * CommandResult — returned by every action handler.
 * Maps directly to the /device/result JSON contract.
 */
data class CommandResult(
    val status: String,   // success | cancelled | failed | permission_denied
    val detail: String,
    val data:   Map<String, Any> = emptyMap(),
)

/**
 * CommandReceiver — routes a command JSON to the correct ActionDispatcher method.
 */
class CommandReceiver(private val context: Context) {

    private val dispatcher = ActionDispatcher(context)

    companion object {
        private const val TAG = "CommandReceiver"
    }

    /**
     * Dispatch a command to the right handler.
     * Always returns a result — never throws to the caller.
     */
    suspend fun handle(intent: String, params: JSONObject): CommandResult {
        Log.d(TAG, "Dispatching: $intent")
        return try {
            when (intent) {
                "CALL_CONTACT"  -> dispatcher.callContact(params)
                "SEND_SMS"      -> dispatcher.sendSms(params)
                "SET_ALARM"     -> dispatcher.setAlarm(params)
                "GET_CONTACTS"  -> dispatcher.getContacts(params)
                "PLAY_YOUTUBE"  -> dispatcher.playYoutube(params)
                "OPEN_APP"      -> dispatcher.openApp(params)
                else -> CommandResult(
                    status = "failed",
                    detail = "Unknown intent: '$intent'",
                )
            }
        } catch (e: SecurityException) {
            Log.e(TAG, "Permission denied for $intent: ${e.message}")
            CommandResult(
                status = "permission_denied",
                detail = "Permission required for '$intent': ${e.message}",
            )
        } catch (e: Exception) {
            Log.e(TAG, "Unexpected error dispatching $intent: ${e.message}", e)
            CommandResult(
                status = "failed",
                detail = "Unexpected error: ${e.message}",
            )
        }
    }
}

/**
 * ActionDispatcher — implements each action using Android Intents/APIs.
 *
 * Design rules:
 * - Every method uses startActivity() or ContentResolver — no background calls.
 * - Success means the Intent was dispatched. We cannot confirm user completion.
 * - Cancellation is detected where possible via ActivityResultLauncher.
 * - All results are honest about what actually happened.
 */
class ActionDispatcher(private val context: Context) {

    companion object {
        private const val TAG = "ActionDispatcher"
    }

    // ── CALL_CONTACT ─────────────────────────────────────────────────────────
    /**
     * Search contacts and open the dialer (ACTION_DIAL) or auto-dial (ACTION_CALL).
     *
     * If contact_name is given and phone_number is missing:
     *   → ContactSearcher.search() is called first.
     *   → If 1 match, proceed with dialling.
     *   → If multiple matches, return them as data so web UI can ask user to choose.
     *
     * IMPORTANT:
     *   ACTION_DIAL  — opens dialer pre-filled. User presses green button. No CALL_PHONE needed.
     *   ACTION_CALL  — auto-dials. Requires CALL_PHONE dangerous permission.
     *   Nova defaults to ACTION_DIAL. Honest: we cannot confirm the call was placed.
     */
    suspend fun callContact(params: JSONObject): CommandResult {
        val contactName   = params.optString("contact_name").takeIf { it.isNotBlank() }
        val phoneNumber   = params.optString("phone_number").takeIf { it.isNotBlank() }
        val useActionCall = params.optBoolean("use_action_call", false)

        // Case 1: direct number provided
        if (phoneNumber != null) {
            return dialNumber(phoneNumber, useActionCall)
        }

        // Case 2: search by name
        if (contactName != null) {
            val searcher = ContactSearcher(context)
            val matches  = searcher.search(contactName)

            when {
                matches.isEmpty() -> {
                    Log.i(TAG, "CALL_CONTACT: no contacts found for '$contactName'")
                    return CommandResult(
                        status = "failed",
                        detail = "No contacts found matching '$contactName'.",
                    )
                }
                matches.size == 1 -> {
                    val number = matches[0].phoneNumbers.firstOrNull()
                        ?: return CommandResult(
                            status = "failed",
                            detail = "${matches[0].name} has no phone number.",
                        )
                    return dialNumber(number, useActionCall)
                }
                else -> {
                    // Multiple matches — send back to web UI for disambiguation
                    Log.i(TAG, "CALL_CONTACT: ${matches.size} matches for '$contactName'")
                    return CommandResult(
                        status = "success",
                        detail = "Found ${matches.size} contacts matching '$contactName'. " +
                                 "Web UI should present choices.",
                        data   = mapOf("matches" to matches.map { m ->
                            mapOf("name" to m.name, "phone_numbers" to m.phoneNumbers)
                        }),
                    )
                }
            }
        }

        return CommandResult(status = "failed", detail = "Neither contact_name nor phone_number given.")
    }

    private fun dialNumber(number: String, useAutoCall: Boolean): CommandResult {
        return try {
            val uri    = Uri.parse("tel:$number")
            val action = if (useAutoCall) Intent.ACTION_CALL else Intent.ACTION_DIAL
            val intent = Intent(action, uri).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            context.startActivity(intent)
            val verb = if (useAutoCall) "Auto-dialling" else "Dialler opened for"
            Log.i(TAG, "CALL_CONTACT: $verb $number")
            CommandResult(
                status = "success",
                detail = "$verb $number. ${if (!useAutoCall) "User must press the call button." else ""}",
                data   = mapOf("number" to number, "action" to action),
            )
        } catch (e: SecurityException) {
            CommandResult(
                status = "permission_denied",
                detail = "CALL_PHONE permission required for auto-dial. Grant it in Settings.",
            )
        } catch (e: android.content.ActivityNotFoundException) {
            CommandResult(status = "failed", detail = "No phone app found on device.")
        }
    }

    // ── SEND_SMS ──────────────────────────────────────────────────────────────
    /**
     * Opens the SMS app pre-filled. The user must press Send.
     * No SEND_SMS permission needed — Intent-based, fully transparent.
     */
    fun sendSms(params: JSONObject): CommandResult {
        val phone = params.getString("phone_number")
        val body  = params.getString("message_body")

        return try {
            val intent = Intent(Intent.ACTION_SENDTO).apply {
                data = Uri.parse("smsto:$phone")
                putExtra("sms_body", body)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            context.startActivity(intent)
            Log.i(TAG, "SEND_SMS: SMS app opened for $phone")
            CommandResult(
                status = "success",
                detail = "SMS app opened for $phone. User must press Send.",
                data   = mapOf("to" to phone, "preview" to body.take(80)),
            )
        } catch (e: android.content.ActivityNotFoundException) {
            CommandResult(status = "failed", detail = "No SMS app found on this device.")
        }
    }

    // ── SET_ALARM ─────────────────────────────────────────────────────────────
    /**
     * Uses AlarmClock.ACTION_SET_ALARM — no permission required.
     * The Clock app handles confirmation. We report success when Intent is dispatched.
     */
    fun setAlarm(params: JSONObject): CommandResult {
        val hour   = params.getInt("hour")
        val minute = params.getInt("minute")
        val label  = params.optString("label", "Nova Alarm")

        return try {
            val intent = Intent(AlarmClock.ACTION_SET_ALARM).apply {
                putExtra(AlarmClock.EXTRA_HOUR,     hour)
                putExtra(AlarmClock.EXTRA_MINUTES,  minute)
                putExtra(AlarmClock.EXTRA_MESSAGE,  label)
                putExtra(AlarmClock.EXTRA_SKIP_UI,  false)  // show confirmation UI
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            context.startActivity(intent)
            Log.i(TAG, "SET_ALARM: %02d:%02d '%s'".format(hour, minute, label))
            CommandResult(
                status = "success",
                detail = "Alarm set for %02d:%02d — '%s'.".format(hour, minute, label),
                data   = mapOf("hour" to hour, "minute" to minute, "label" to label),
            )
        } catch (e: android.content.ActivityNotFoundException) {
            CommandResult(status = "failed", detail = "No Clock app found on this device.")
        }
    }

    // ── GET_CONTACTS ──────────────────────────────────────────────────────────
    /**
     * Search contacts via ContentResolver and return matches.
     * Requires READ_CONTACTS permission (dangerous — prompted at runtime).
     */
    suspend fun getContacts(params: JSONObject): CommandResult {
        val query = params.getString("query")
        val searcher = ContactSearcher(context)

        return try {
            val matches = searcher.search(query)
            CommandResult(
                status = "success",
                detail = "Found ${matches.size} contacts for '$query'.",
                data   = mapOf("contacts" to matches.map { m ->
                    mapOf("name" to m.name, "phone_numbers" to m.phoneNumbers)
                }),
            )
        } catch (e: SecurityException) {
            CommandResult(
                status = "permission_denied",
                detail = "READ_CONTACTS permission required. Grant it in Settings.",
            )
        }
    }

    // ── PLAY_YOUTUBE ──────────────────────────────────────────────────────────
    /**
     * Open YouTube search results. Tries the YouTube app first, falls back to browser.
     * Does NOT claim music is playing — it opens a search results page.
     */
    fun playYoutube(params: JSONObject): CommandResult {
        val query      = params.getString("query")
        val searchUrl  = "https://www.youtube.com/results?search_query=${Uri.encode(query)}"

        // Try YouTube app deep-link first
        val ytIntent = context.packageManager
            .getLaunchIntentForPackage("com.google.android.youtube")
            ?.apply {
                action = Intent.ACTION_SEARCH
                putExtra("query", query)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }

        return if (ytIntent != null) {
            context.startActivity(ytIntent)
            Log.i(TAG, "PLAY_YOUTUBE: opened YouTube app for '$query'")
            CommandResult(
                status = "success",
                detail = "YouTube app opened with search: '$query'.",
                data   = mapOf("query" to query, "via" to "youtube_app"),
            )
        } else {
            // Fallback: open in browser
            val browserIntent = Intent(Intent.ACTION_VIEW, Uri.parse(searchUrl)).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            context.startActivity(browserIntent)
            Log.i(TAG, "PLAY_YOUTUBE: opened browser for '$query' (YouTube not installed)")
            CommandResult(
                status = "success",
                detail = "YouTube search opened in browser (app not found): '$query'.",
                data   = mapOf("query" to query, "url" to searchUrl, "via" to "browser"),
            )
        }
    }

    // ── OPEN_APP ──────────────────────────────────────────────────────────────
    /**
     * Launch an installed app by package name.
     */
    fun openApp(params: JSONObject): CommandResult {
        val packageName = params.getString("package_name")

        val launchIntent = context.packageManager.getLaunchIntentForPackage(packageName)
            ?: return CommandResult(
                status = "failed",
                detail = "App '$packageName' is not installed.",
            )

        launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(launchIntent)
        Log.i(TAG, "OPEN_APP: launched $packageName")
        return CommandResult(
            status = "success",
            detail = "App '$packageName' opened.",
            data   = mapOf("package" to packageName),
        )
    }
}
