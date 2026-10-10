package com.xcover.rootmanager;

import android.app.Activity;
import android.os.Bundle;
import android.os.StrictMode;
import android.net.LocalSocket;
import android.net.LocalSocketAddress;
import android.widget.*;
import android.view.View;
import java.io.*;
import java.nio.charset.StandardCharsets;

public class MainActivity extends Activity {
    private final String SOCKET = "/data/local/rootmanager/control.sock";
    private TextView status;
    private EditText uidField;
    private LinearLayout pendingBox;

    private String command(String request) throws IOException {
        LocalSocket sock = new LocalSocket();
        try {
            sock.connect(new LocalSocketAddress(SOCKET, LocalSocketAddress.Namespace.FILESYSTEM));
            sock.setSoTimeout(2500);
            sock.getOutputStream().write((request + "\n").getBytes(StandardCharsets.UTF_8));
            sock.getOutputStream().flush();
            ByteArrayOutputStream bytes = new ByteArrayOutputStream();
            byte[] buf = new byte[1024];
            while (bytes.size() < 32768) {
                int n = sock.getInputStream().read(buf);
                if (n < 0) break;
                bytes.write(buf, 0, n);
                String data = bytes.toString("UTF-8");
                if (data.contains("_END\n") || data.contains("_OK ") || data.startsWith("DENY ") || data.startsWith("ERROR ")) break;
            }
            return bytes.toString("UTF-8").trim();
        } finally { sock.close(); }
    }
    private void request(final String cmd, final boolean refresh) {
        status.setText("Skickar " + cmd + "...");
        new Thread(() -> {
            String result;
            try { result = command(cmd); } catch (Exception ex) { result = "Anslutningsfel: " + ex.getMessage(); }
            final String output = result;
            runOnUiThread(() -> {
                status.setText(output);
                if (refresh) refreshPending();
            });
        }).start();
    }
    private void refreshPending() {
        new Thread(() -> {
            String data;
            try { data = command("PENDING"); } catch (Exception ex) { data = "Anslutningsfel: " + ex.getMessage(); }
            final String result = data;
            runOnUiThread(() -> {
                pendingBox.removeAllViews();
                if (!result.startsWith("PENDING_BEGIN")) { status.setText(result); return; }
                for (String line : result.split("\\n")) {
                    if (!line.matches("[0-9]+")) continue;
                    long parsed;
                    try { parsed = Long.parseLong(line); } catch (Exception e) { continue; }
                    if (parsed < 10000 || parsed >= 100000) continue;
                    final String uid = line;
                    TextView title = new TextView(this); title.setText("Väntande app-UID: " + uid); title.setTextSize(18);
                    pendingBox.addView(title);
                    LinearLayout buttons = new LinearLayout(this);
                    Button allow = new Button(this); allow.setText("Tillåt");
                    allow.setOnClickListener(v -> request("ALLOW " + uid, true));
                    Button deny = new Button(this); deny.setText("Neka");
                    deny.setOnClickListener(v -> request("DENY " + uid, true));
                    buttons.addView(allow); buttons.addView(deny); pendingBox.addView(buttons);
                }
            });
        }).start();
    }
    @Override public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        LinearLayout layout = new LinearLayout(this); layout.setOrientation(LinearLayout.VERTICAL); layout.setPadding(24,24,24,24);
        ScrollView scroll = new ScrollView(this); scroll.addView(layout); setContentView(scroll);
        TextView heading = new TextView(this); heading.setText("XCover Root Manager"); heading.setTextSize(24); layout.addView(heading);
        TextView warning = new TextView(this); warning.setText("TESTVERSION. UID-baserade regler: installera inte om appar utan att granska tidigare tillstånd. Tillåt endast appar du litar på."); layout.addView(warning);
        status = new TextView(this); status.setText("Ansluter till Rootbroker..."); layout.addView(status);
        Button refresh = new Button(this); refresh.setText("Uppdatera väntande förfrågningar"); refresh.setOnClickListener(v -> refreshPending()); layout.addView(refresh);
        pendingBox = new LinearLayout(this); pendingBox.setOrientation(LinearLayout.VERTICAL); layout.addView(pendingBox);
        uidField = new EditText(this); uidField.setHint("App-UID, t.ex. 10192"); uidField.setInputType(2); layout.addView(uidField);
        LinearLayout actions = new LinearLayout(this); layout.addView(actions);
        for (String verb : new String[]{"ALLOW", "DENY", "ASK"}) {
            Button b = new Button(this); b.setText(verb.equals("ALLOW") ? "Tillåt UID" : verb.equals("DENY") ? "Neka UID" : "Fråga igen");
            b.setOnClickListener(v -> {
                String uid = uidField.getText().toString().trim();
                if (!uid.matches("[0-9]+")) { status.setText("Ange numeriskt UID"); return; }
                long id; try { id = Long.parseLong(uid); } catch(Exception e) { status.setText("Ogiltigt UID"); return; }
                if(id < 10000 || id >= 100000) { status.setText("UID utanför tillåtet intervall"); return; }
                request(verb + " " + uid, true);
            });
            actions.addView(b);
        }
        Button list = new Button(this); list.setText("Visa tillåtna UID"); list.setOnClickListener(v -> request("LIST", false)); layout.addView(list);
        Button denied = new Button(this); denied.setText("Visa nekade UID"); denied.setOnClickListener(v -> request("DENIED", false)); layout.addView(denied);
        int uid = getIntent().getIntExtra("uid", -1);
        if(uid >= 10000) uidField.setText(Integer.toString(uid));
        refreshPending();
    }
}
