package hidden;

import fixture.OrderRecord;
import fixture.ReportService;
import java.util.Collections;
import org.junit.Assert;
import org.junit.Test;

public class StreamHiddenTest {
    @Test
    public void missingRegionIsZero() {
        Assert.assertEquals(
            0,
            new ReportService().totalForRegion(Collections.emptyList(), "north")
        );
    }
}
