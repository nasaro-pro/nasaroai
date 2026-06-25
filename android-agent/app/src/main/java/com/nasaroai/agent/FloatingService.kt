package com.nasaroai.agent

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.ActivityNotFoundException
import android.content.Intent
import android.graphics.PixelFormat
import android.net.Uri
import android.os.Build
import android.os.IBinder
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

    private lateinit var wm: WindowManager
    private var floatView: View? = null
    private var panelView: View? = null

    private val CHANNEL_ID = "nasaroai_float"
    private val NOTIF_ID   = 1001
    private val NASAROAI_URL  = "https://nasaroai.onrender.com/?source=app"

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
        startForeground(NOTIF_ID, buildNotification())
        showButton()
    }

    override fun onDestroy() {
        super.onDestroy()
        floatView?.let { wm.removeView(it) }
        panelView?.let { wm.removeView(it) }
        floatView = null
        panelView = null
    }

    private fun showButton() {
        wm = getSystemService(WINDOW_SERVICE) as WindowManager

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

    private fun openNasaroAI() {
        val uri = Uri.parse(NASAROAI_URL)
        // Quetta Browser 먼저 시도
        try {
            startActivity(Intent(Intent.ACTION_VIEW, uri).apply {
                setPackage("net.quetta.browser")
                flags = Intent.FLAG_ACTIVITY_NEW_TASK
            })
        } catch (e: ActivityNotFoundException) {
            // 어떤 브라우저든 열기 (폴백)
            try {
                startActivity(Intent(Intent.ACTION_VIEW, uri).apply {
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK
                })
            } catch (_: Exception) {}
        }
    }

    private fun togglePanel() {
        if (panelView != null) {
            panelView?.let { wm.removeView(it) }
            panelView = null
            return
        }
        showPanel()
    }

    @Suppress("SetJavaScriptEnabled")
    private fun showPanel() {
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
            loadUrl(NASAROAI_URL)
            setBackgroundColor(0xFFFFFFFF.toInt())
        }

        panelView = web

        val dm = resources.displayMetrics
        val width = (dm.widthPixels * 0.92f).toInt().coerceAtLeast(720)
        val height = (dm.heightPixels * 0.70f).toInt().coerceAtLeast(900)

        val params = WindowManager.LayoutParams(
            width,
            height,
            type,
            WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = ((dm.widthPixels - width) / 2).coerceAtLeast(8)
            y = (dm.heightPixels * 0.12f).toInt().coerceAtLeast(8)
        }

        wm.addView(panelView, params)
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
