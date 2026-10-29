package com.nova.companion

import android.content.Context
import android.provider.ContactsContract
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * A single contact result.
 */
data class ContactMatch(
    val name:         String,
    val phoneNumbers: List<String>,
)

/**
 * ContactSearcher — queries Android Contacts via ContentResolver.
 *
 * Requires READ_CONTACTS permission (dangerous, must be granted at runtime).
 * The caller (ActionDispatcher) catches SecurityException if permission is missing.
 *
 * Search strategy:
 *   1. Query ContactsContract.CommonDataKinds.Phone WHERE display_name LIKE '%query%'
 *   2. Group results by contact ID to avoid duplicate entries for multi-number contacts
 *   3. Return List<ContactMatch> sorted by name
 *
 * The query is a simple LIKE — not a fuzzy match.  For better results on large
 * contact lists, a proper search index (e.g. FTS via SQLite) would be needed.
 */
class ContactSearcher(private val context: Context) {

    companion object {
        private const val TAG      = "ContactSearcher"
        private const val MAX_RESULTS = 20   // safety cap — never return hundreds of contacts
    }

    /**
     * Search contacts by display name.
     * Returns up to MAX_RESULTS matches, each with all associated phone numbers.
     * Throws SecurityException if READ_CONTACTS permission is not granted.
     */
    suspend fun search(query: String): List<ContactMatch> = withContext(Dispatchers.IO) {
        Log.d(TAG, "Searching contacts: query='$query'")

        val projection = arrayOf(
            ContactsContract.CommonDataKinds.Phone.CONTACT_ID,
            ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
            ContactsContract.CommonDataKinds.Phone.NUMBER,
        )

        val selection = "${ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME} LIKE ?"
        val args      = arrayOf("%${query.trim()}%")
        val sortOrder = "${ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME} ASC"

        // Group contacts by ID to merge multiple numbers
        val grouped = LinkedHashMap<String, MutableList<String>>()  // contactId → [numbers]
        val names   = LinkedHashMap<String, String>()               // contactId → displayName

        context.contentResolver.query(
            ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
            projection,
            selection,
            args,
            sortOrder,
        )?.use { cursor ->
            val idCol    = cursor.getColumnIndexOrThrow(ContactsContract.CommonDataKinds.Phone.CONTACT_ID)
            val nameCol  = cursor.getColumnIndexOrThrow(ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME)
            val numCol   = cursor.getColumnIndexOrThrow(ContactsContract.CommonDataKinds.Phone.NUMBER)

            while (cursor.moveToNext() && grouped.size < MAX_RESULTS) {
                val id     = cursor.getString(idCol)
                val name   = cursor.getString(nameCol) ?: continue
                val number = cursor.getString(numCol)?.trim() ?: continue

                if (number.isBlank()) continue

                names.getOrPut(id) { name }
                grouped.getOrPut(id) { mutableListOf() }.add(number)
            }
        }

        val results = grouped.map { (id, numbers) ->
            ContactMatch(
                name         = names[id] ?: "Unknown",
                phoneNumbers = numbers.distinct(),
            )
        }

        Log.i(TAG, "Contact search '$query' → ${results.size} result(s)")
        results
    }
}
