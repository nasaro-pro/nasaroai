package com.arenax.agent

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.SwitchCompat

class MainActivity : AppCompatActivity() {

    private lateinit var btnToggle: Button
    private lateinit var tvStatus: TextView
    private lateinit var switchBoot: SwitchCompat

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        btnToggle  = findViewById(R.id.btnToggle)
        tvStatus   = findViewById(R.id.tvStatus)
        switchBoot = findViewById(R.id.switchBoot)

        val prefs = getSharedPreferences("arenax_float", MODE_PRIVATE)
        switchBoot.isChecked = prefs.getBoolean("auto_start", false)
        switchBoot.setOnCheckedChangeListener { _, checked ->
            prefs.edit().putBoolean("auto_start", checked).apply()
        }

        btnToggle.setOnClickListener {
            if (!Settings.canDrawOverlays(this)) {
                startActivity(
                    Intent(
                        Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                        Uri.parse("package:$packageName")
                    )
                )
            } else {
                toggleService()
            }
        }
    }

    override fun onResume() {
        super.onResume()
        updateUI()
    }

    private fun updateUI() {
        when {
            !Settings.canDrawOverlays(this) -> {
                tvStatus.text = "⚠ 오버레이 권한이 필요합니다"
                btnToggle.text = "권한 허용하기"
            }
            isServiceRunning() -> {
                tvStatus.text = "● 플로팅 버튼 실행 중"
                btnToggle.text = "버튼 끄기"
            }
            else -> {
                tvStatus.text = "○ 버튼 꺼짐"
                btnToggle.text = "버튼 켜기"
            }
        }
    }

    private fun toggleService() {
        val intent = Intent(this, FloatingService::class.java)
        if (isServiceRunning()) {
            stopService(intent)
        } else {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(intent)
            } else {
                startService(intent)
            }
        }
        btnToggle.postDelayed({ updateUI() }, 400)
    }

    @Suppress("DEPRECATION")
    private fun isServiceRunning(): Boolean {
        val am = getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        return am.getRunningServices(Int.MAX_VALUE).any {
            it.service.className == FloatingService::class.java.name
        }
    }
}
