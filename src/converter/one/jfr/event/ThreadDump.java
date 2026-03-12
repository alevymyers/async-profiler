package one.jfr.event;

import one.jfr.JfrReader;

public class ThreadDump extends Event {
    public final String result;

    public ThreadDump(JfrReader jfr) {
        super(jfr.getVarlong(), 0, 0);
        result = jfr.getString();
    }
}
