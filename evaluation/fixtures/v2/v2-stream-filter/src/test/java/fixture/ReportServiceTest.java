package fixture;

import static org.junit.Assert.assertEquals;
import java.util.Arrays;
import org.junit.Test;

public class ReportServiceTest {
    @Test
    public void totalsMatchingRegion() {
        assertEquals(
            30,
            new ReportService().totalForRegion(
                Arrays.asList(new OrderRecord("west", 10), new OrderRecord("east", 20)),
                "west"
            )
        );
    }
}
