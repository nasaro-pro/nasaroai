package com.nasaroai.agent

import android.app.AlertDialog
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.graphics.PixelFormat
import android.os.Build
import android.os.IBinder
import android.provider.Settings
import android.webkit.JavascriptInterface
import android.view.Gravity
import android.view.LayoutInflater
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import androidx.core.app.NotificationCompat

class FloatingService : Service() {

    companion object {
        const val ACTION_OPEN_PANEL = "com.nasaroai.agent.OPEN_PANEL"
        const val ACTION_SHOW_LAUNCHER = "com.nasaroai.agent.SHOW_LAUNCHER"
        const val ACTION_STOP_AGENT = "com.nasaroai.agent.STOP_AGENT"
    }

    private lateinit var wm: WindowManager
    private var floatView: View? = null
    private var panelView: View? = null

    private val CHANNEL_ID = "nasaroai_float"
    private val NOTIF_ID   = 1001

    private fun nasaroBaseUrl(): String {
        var url = getSharedPreferences("nasaro_app", MODE_PRIVATE)
            .getString("server_url", null)?.trim().orEmpty()
        if (url.isEmpty()) return ""
        if (!url.startsWith("http")) url = "https://$url"
        return url.trimEnd('/')
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        wm = getSystemService(WINDOW_SERVICE) as WindowManager
        createChannel()
        startForeground(NOTIF_ID, buildNotification())
        AccessibilityAgentService.confirmHandler = { message ->
            var approved = false
            val latch = java.util.concurrent.CountDownLatch(1)
            android.os.Handler(mainLooper).post {
                AlertDialog.Builder(this@FloatingService)
                    .setTitle("확인 필요")
                    .setMessage(message)
                    .setCancelable(false)
                    .setPositiveButton("허용") { _, _ ->
                        approved = true
                        latch.countDown()
                    }
                    .setNegativeButton("취소") { _, _ -> latch.countDown() }
                    .show()
            }
            latch.await(60, java.util.concurrent.TimeUnit.SECONDS)
            approved
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_OPEN_PANEL -> {
                getSharedPreferences("nasaroai_float", MODE_PRIVATE)
                    .edit()
                    .putBoolean("agent_enabled", true)
                    .apply()
                hideButton()
                showPanel()
            }
            ACTION_SHOW_LAUNCHER -> {
                getSharedPreferences("nasaroai_float", MODE_PRIVATE)
                    .edit()
                    .putBoolean("agent_enabled", true)
                    .apply()
                hidePanel()
                showButton()
            }
            ACTION_STOP_AGENT -> stopSelf()
            else -> {
                if (getSharedPreferences("nasaroai_float", MODE_PRIVATE).getBoolean("agent_enabled", false)) {
                    showButton()
                }
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        AccessibilityAgentService.confirmHandler = null
        super.onDestroy()
        floatView?.let { wm.removeView(it) }
        panelView?.let { wm.removeView(it) }
        floatView = null
        panelView = null
    }

    private fun showButton() {
        if (floatView != null) return

        floatView = LayoutInflater.from(this).inflate(R.layout.floating_button, null)

        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        else
            @Suppress("DEPRECATION") WindowManager.LayoutParams.TYPE_PHONE

        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            type,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            val prefs = getSharedPreferences("nasaroai_float", MODE_PRIVATE)
            x = prefs.getInt("btn_x", 30)
            y = prefs.getInt("btn_y", 400)
        }

        wm.addView(floatView, params)
        attachTouchListener(params)
    }

    private fun hideButton() {
        floatView?.let { runCatching { wm.removeView(it) } }
        floatView = null
    }

    private fun attachTouchListener(params: WindowManager.LayoutParams) {
        var startX = 0; var startY = 0
        var touchX = 0f; var touchY = 0f
        var dragging = false; var downAt = 0L

        floatView!!.setOnTouchListener { _, ev ->
            when (ev.action) {
                MotionEvent.ACTION_DOWN -> {
                    startX = params.x; startY = params.y
                    touchX = ev.rawX; touchY = ev.rawY
                    dragging = false; downAt = System.currentTimeMillis()
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = (ev.rawX - touchX).toInt()
                    val dy = (ev.rawY - touchY).toInt()
                    if (!dragging && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) dragging = true
                    if (dragging) {
                        params.x = startX + dx
                        params.y = startY + dy
                        wm.updateViewLayout(floatView, params)
                    }
                    true
                }
                MotionEvent.ACTION_UP -> {
                    val elapsed = System.currentTimeMillis() - downAt
                    if (!dragging) {
                        if (elapsed > 600L) {
                            stopSelf()           // 길게 누르면 종료
                        } else {
                            togglePanel()        // 탭하면 오버레이 질문창 토글
                        }
                    } else {
                        // 위치 저장
                        getSharedPreferences("nasaroai_float", MODE_PRIVATE).edit()
                            .putInt("btn_x", params.x)
                            .putInt("btn_y", params.y)
                            .apply()
                    }
                    true
                }
                else -> false
            }
        }
    }

    private fun togglePanel() {
        if (panelView != null) {
            hidePanel()
            return
        }
        hideButton()
        showPanel()
    }

    private fun hidePanel() {
        panelView?.let { runCatching { wm.removeView(it) } }
        panelView = null
    }

    @Suppress("SetJavaScriptEnabled")
    private fun showPanel() {
        if (panelView != null) return
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        else
            @Suppress("DEPRECATION") WindowManager.LayoutParams.TYPE_PHONE

        val web = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.cacheMode = WebSettings.LOAD_DEFAULT
            settings.userAgentString = settings.userAgentString + " NasaroAIApp"
            webChromeClient = WebChromeClient()
            addJavascriptInterface(OverlayBridge(), "NasaroAndroidAgent")
            val base = nasaroBaseUrl()
            if (base.isNotEmpty()) loadUrl("$base/?source=app&agent_overlay=1")
            setBackgroundColor(0xFFFFFFFF.toInt())
        }

        panelView = web

        val dm = resources.displayMetrics
        val width = WindowManager.LayoutParams.MATCH_PARENT
        val height = (dm.heightPixels * 0.52f).toInt().coerceAtLeast((360 * dm.density).toInt())

        val params = WindowManager.LayoutParams(
            width,
            height,
            type,
            WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.BOTTOM or Gravity.START
            x = 0
            y = 0
            softInputMode = WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE
        }

        wm.addView(panelView, params)
    }

    inner class OverlayBridge {
        @JavascriptInterface
        fun openAgent() {
            // 이미 오버레이 패널 안이므로 별도 동작 없음.
        }

        @JavascriptInterface
        fun minimizeAgent() {
            android.os.Handler(mainLooper).post {
                hidePanel()
                showButton()
            }
        }

        @JavascriptInterface
        fun stopAgent() {
            android.os.Handler(mainLooper).post {
                getSharedPreferences("nasaroai_float", MODE_PRIVATE)
                    .edit()
                    .putBoolean("agent_enabled", false)
                    .apply()
                stopSelf()
            }
        }

        @JavascriptInterface
        fun isAccessibilityReady(): Boolean {
            return AccessibilityAgentService.isEnabled(this@FloatingService)
        }

        @JavascriptInterface
        fun openAccessibilitySettings() {
            android.os.Handler(mainLooper).post {
                startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS).apply {
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK
                })
            }
        }

        @JavascriptInterface
        fun openOverlaySettings() {
            android.os.Handler(mainLooper).post {
                startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION).apply {
                    data = android.net.Uri.parse("package:$packageName")
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK
                })
            }
        }

        @JavascriptInterface
        fun canDrawOverlay(): Boolean {
            return android.provider.Settings.canDrawOverlays(this@FloatingService)
        }

        @JavascriptInterface
        fun runNativeTask(task: String): String {
            val needsForeground = needsForegroundTask(task)
            val latch = java.util.concurrent.CountDownLatch(1)
            var result = ""
            android.os.Handler(mainLooper).post {
                val panelWasOpen = panelView != null
                if (needsForeground && panelWasOpen) hidePanel()
                android.os.Handler(mainLooper).postDelayed({
                    result = AccessibilityAgentService.performTask(task)
                    if (needsForeground && panelWasOpen) showPanel()
                    latch.countDown()
                }, if (needsForeground) 380L else 0L)
            }
            val stepCount = task.split(Regex("(?:\\s*해서\\s*|\\s*하고\\s*|\\s*후에\\s*|\\s*다음\\s*|\\s*그리고\\s*|\\s*,\\s*)"))
                .map { it.trim() }
                .count { it.isNotEmpty() }
                .coerceAtLeast(1)
            val timeoutSec = (stepCount * 8L).coerceIn(12L, 45L)
            latch.await(timeoutSec, java.util.concurrent.TimeUnit.SECONDS)
            return result.ifBlank { "Android 접근성 작업 시간이 초과되었습니다. 다시 시도해주세요." }
        }
    }

    private fun needsForegroundTask(task: String): Boolean {
        val lower = task.lowercase()
        if (lower.contains("홈") || lower.contains("home")) return false
        if (lower.contains("뒤로") || lower.contains("back")) return false
        if (lower.contains("최근") || lower.contains("recent")) return false
        return true
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val ch = NotificationChannel(
                CHANNEL_ID, "Nasaro AI 에이전트", NotificationManager.IMPORTANCE_LOW
            ).apply { description = "플로팅 버튼 실행 중" }
            getSystemService(NotificationManager::class.java).createNotificationChannel(ch)
        }
    }

    private fun buildNotification(): Notification {
        val pi = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notif)
            .setContentTitle("Nasaro AI 에이전트")
            .setContentText("탭해서 질문창 열기 · 길게 누르면 닫기")
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setOngoing(true)
            .setContentIntent(pi)
            .build()
    }
}
