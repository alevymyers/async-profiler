import jdk.jfr.*;
import java.nio.file.Path;
import java.time.Duration;

public class GenerateJfr {

    static void recurse(int depth) {
        if(depth == 0) {
            try {
                Thread.sleep(Long.MAX_VALUE); 
                return;
            }
            catch (InterruptedException e ) {}
        }
        recurse(depth - 1);
    }

    public static void main(String[] argv) throws Exception {
        for(int i = 0; i < 100; i++) {
            new Thread(() -> recurse(10_000), "test-thread" + i).start();
        }
        Path path = Path.of("large-thread-dump.jfr");
        try (Recording r = new Recording()) {
            System.out.println("starting recording");
            r.enable("jdk.ThreadDump").withPeriod(Duration.ofSeconds(1));
            r.start();
            Thread.sleep(2000);
            r.stop();
            r.dump(path);
        }
        System.out.println("finished recording");
        System.exit(0);
    }
}
