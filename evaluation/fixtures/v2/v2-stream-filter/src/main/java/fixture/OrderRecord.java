package fixture;

public class OrderRecord {
    private final String region;
    private final int amount;

    public OrderRecord(String region, int amount) {
        this.region = region;
        this.amount = amount;
    }

    public String getRegion() {
        return region;
    }

    public int getAmount() {
        return amount;
    }
}
