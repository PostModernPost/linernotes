package app.linernotes

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import org.json.JSONObject
import java.io.ByteArrayOutputStream

/**
 * Runs while the app is connected to the PC, so it keeps working with the screen off:
 *  - watches Liner Notes on the PC (about once a second)
 *  - turns the screen back on when the next song starts, and lets it go dark when the PC says so
 *  - records with this phone's microphone when the PC listens through the phone (Settings > Microphone > Listen with)
 */
class LinkService : Service() {

    companion object {
        const val CHANNEL = "link"
        const val EXTRA_URL = "url"
        @Volatile var running = false
        @Volatile var serverUrl: String? = null
        /** The screen should stay dark (nothing playing for a while, or the PC is asleep). MainActivity reads this. */
        @Volatile var screenOffWanted = false
        /** Liner Notes (on the PC, or on this phone) answered the last poll. */
        @Volatile var reachable = true
        var onState: ((JSONObject?) -> Unit)? = null      // MainActivity listens for the PC's state

        fun start(c: Context, url: String) {
            val i = Intent(c, LinkService::class.java).putExtra(EXTRA_URL, url)
            try {
                if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i) else c.startService(i)
            } catch (e: Exception) { }
        }

        fun stop(c: Context) { c.stopService(Intent(c, LinkService::class.java)) }
    }

    private var thread: Thread? = null
    @Volatile private var stopping = false
    private var recorder: AudioRecord? = null
    private var gain = 1                // UNPROCESSED has no mic boost: room music comes in around 0.001, too quiet to count as music
    private val sr = 16000
    private var micMode = "off"
    private var lastVersion = -1
    private var lastPlaying = false
    private var lastOff = false
    private var awayDark = false
    private var failSince = 0L
    private var screenWake: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        intent?.getStringExtra(EXTRA_URL)?.let { serverUrl = it }
        goForeground()
        if (thread == null) {
            stopping = false
            running = true
            thread = Thread { loop() }.apply { isDaemon = true; start() }
        }
        return START_STICKY
    }

    private fun canRecord() = checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    private fun goForeground() {
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Connected to your PC", NotificationManager.IMPORTANCE_MIN).apply {
                description = "Shown while Liner Notes follows the PC with the screen off"
                setShowBadge(false)
            })
        }
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n: Notification = (if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, CHANNEL) else @Suppress("DEPRECATION") Notification.Builder(this))
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("Liner Notes")
            .setContentText(if (micMode != "off") "Listening for records with this phone" else if (Engine.isLocal(serverUrl)) "Liner Notes is running" else "Following Liner Notes on your PC")
            .setContentIntent(open)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 29) {
            var type = 0
            if (Build.VERSION.SDK_INT >= 34) type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
            if (canRecord()) type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            try { startForeground(1, n, type) } catch (e: Exception) { try { startForeground(1, n) } catch (e2: Exception) { } }
        } else {
            startForeground(1, n)
        }
    }

    private fun loop() {
        var nextProbe = 0L
        var lastUrl: String? = null
        while (!stopping) {
            val url = serverUrl
            if (url == null) { Thread.sleep(1000); continue }
            if (url != lastUrl) {
                // Switched to another Liner Notes (the PC went to sleep and the phone carries on by itself):
                // that's not the PC coming back, so the screen stays as it is.
                if (lastUrl != null) { awayDark = false; failSince = 0L }
                lastUrl = url
            }
            val r = Link.call(this, url, "/api/now", timeoutMs = 2500)
            val now = System.currentTimeMillis()
            reachable = r != null && r.first == 200
            if (r == null || r.first != 200) {
                // The PC is off, asleep or out of reach. After 20 s, let the screen go dark (it comes back with the PC).
                if (failSince == 0L) failSince = now
                if (now - failSince > 20_000 && !awayDark) { awayDark = true; screenOffWanted = true; onState?.invoke(null) }
                stopMic()
                Thread.sleep(3000)
                continue
            }
            val pcWasAway = awayDark
            awayDark = false
            failSince = 0L
            val j = try { JSONObject(r.second) } catch (e: Exception) { null }
            if (j != null) {
                handleScreen(j, pcWasAway)
                onState?.invoke(j)
                val mic = j.optJSONObject("mic")
                val mode = mic?.optString("mode", "off") ?: "off"
                if (mode != micMode) {
                    micMode = mode
                    if (mode == "off") stopMic()
                    goForeground()                          // refresh the notification text
                }
                when (mode) {
                    "active" -> { streamFor(url, 1000, "") }
                    "meter" -> { streamFor(url, 1000, "meter") }
                    "standby" -> {
                        if (now >= nextProbe) {
                            val secs = mic?.optDouble("seconds", 2.0) ?: 2.0
                            val every = mic?.optDouble("every", 20.0) ?: 20.0
                            probe(url, secs)
                            nextProbe = System.currentTimeMillis() + (every * 1000).toLong()
                        } else Thread.sleep(1000)
                    }
                    else -> Thread.sleep(1000)
                }
            } else Thread.sleep(1000)
        }
    }

    private fun handleScreen(j: JSONObject, pcWasAway: Boolean) {
        val version = j.optInt("version", -1)
        val track = j.optJSONObject("track")
        val playing = track != null && track.optString("status") == "playing"
        val newSong = playing && version != lastVersion && (!lastPlaying || version != lastVersion)
        lastVersion = version
        lastPlaying = playing
        val off = j.optString("phone_screen", "on") == "off"
        val turnedOff = off && !lastOff        // only when the PC first says so, so a tap on the screen can still wake it
        lastOff = off
        if (playing && (newSong || pcWasAway)) {
            screenOffWanted = false
            wakeScreen()
        } else if (turnedOff) {
            screenOffWanted = true
        } else if (pcWasAway) {
            screenOffWanted = false
            wakeScreen()
        }
    }

    @SuppressLint("WakelockTimeout")
    @Suppress("DEPRECATION")
    private fun wakeScreen() {
        if (!Pro.unlocked(this)) return          // waking the screen for the next song is part of Liner Notes Plus
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        if (pm.isInteractive) return
        try {
            // Turns the screen on like pressing the power button; MainActivity shows over the lock screen
            screenWake?.let { if (it.isHeld) it.release() }
            screenWake = pm.newWakeLock(PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP or
                    PowerManager.ON_AFTER_RELEASE, "linernotes:wake").apply { acquire(10_000) }
        } catch (e: Exception) { }
        try {
            startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT))
        } catch (e: Exception) { }
    }

    // ---------------------------------------------------------------- the phone's microphone
    // Same as the phone page used to do: short samples on standby, a steady stream while music plays.
    @SuppressLint("MissingPermission")
    private fun ensureRecorder(): AudioRecord? {
        if (!canRecord()) return null
        recorder?.let { return it }
        val min = AudioRecord.getMinBufferSize(sr, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        // UNPROCESSED skips the phone's voice noise-suppression, which can eat quiet music
        val sources = if (Build.VERSION.SDK_INT >= 24) listOf(MediaRecorder.AudioSource.UNPROCESSED, MediaRecorder.AudioSource.MIC)
                      else listOf(MediaRecorder.AudioSource.MIC)
        for (src in sources) {
            try {
                val r = AudioRecord(src, sr, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(min, sr * 2 * 4))   // 4 s, so nothing is lost while it talks to the PC
                if (r.state == AudioRecord.STATE_INITIALIZED) {
                    r.startRecording()
                    recorder = r
                    gain = if (src == MediaRecorder.AudioSource.UNPROCESSED) 8 else 1     // +18 dB, about what MIC's own boost adds
                    return r
                }
                r.release()
            } catch (e: Exception) { }
        }
        return null
    }

    private fun stopMic() {
        try { recorder?.stop() } catch (e: Exception) { }
        try { recorder?.release() } catch (e: Exception) { }
        recorder = null
    }

    private fun read(ms: Int): ByteArray? {
        val r = ensureRecorder() ?: return null
        val want = sr * 2 * ms / 1000
        val out = ByteArrayOutputStream(want)
        val buf = ByteArray(minOf(want, 3200))
        while (out.size() < want && !stopping) {
            val n = r.read(buf, 0, minOf(buf.size, want - out.size()))
            if (n <= 0) break
            out.write(buf, 0, n)
        }
        val pcm = out.toByteArray()   // 16-bit little-endian mono at 16 kHz, what the PC expects
        if (gain != 1) {
            for (i in 0 until pcm.size - 1 step 2) {
                val v = ((pcm[i + 1].toInt() shl 8) or (pcm[i].toInt() and 0xff)) * gain
                val c = v.coerceIn(-32768, 32767)
                pcm[i] = c.toByte(); pcm[i + 1] = (c shr 8).toByte()
            }
        }
        return pcm
    }

    private fun upload(url: String, pcm: ByteArray, query: String) {
        Link.call(this, url, "/api/audio$query", "POST", bytes = pcm, contentType = "application/octet-stream", timeoutMs = 3000)
    }

    /** Record about [ms] and send it (used while the PC is actively listening, or for the mic meter). */
    private fun streamFor(url: String, ms: Int, kind: String) {
        if (kind == "meter") {
            // the mic meter in the PC's Settings: five small pieces a second so it moves live
            repeat(5) { read(200)?.let { upload(url, it, "?probe=0&meter=1") } }
        } else {
            read(ms)?.let { upload(url, it, "?probe=0") } ?: Thread.sleep(ms.toLong())
        }
    }

    private fun probe(url: String, seconds: Double) {
        val pcm = read((seconds * 1000).toInt()) ?: return
        upload(url, pcm, "?probe=1")
        stopMic()                     // the mic goes off between samples
    }

    override fun onDestroy() {
        stopping = true
        running = false
        stopMic()
        screenWake?.let { if (it.isHeld) it.release() }
        thread = null
        super.onDestroy()
    }
}
